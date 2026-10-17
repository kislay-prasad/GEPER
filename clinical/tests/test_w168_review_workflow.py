"""
clinical/tests/test_w168_review_workflow.py
───────────────────────────────────────────
Stage 3: the review surface (`/review/*`) and the COMPLETE WORKFLOW, over HTTP,
against PostgreSQL.

    order -> sample receipt -> QC -> interpretation -> review -> approval
          -> release -> the patient receives it

THE ONE STEP THAT IS NOT AN HTTP CALL, AND WHY. The interpretation is recorded
by the background worker as the organisation's System principal
(`geper/api/submission_worker.py` -> `record_pipeline_result`), after it has run
the pipeline. That method takes a server filesystem path and a hash, so it is
deliberately not an HTTP route. `pipeline_step()` below does exactly what the
worker does -- a System session and `record_pipeline_result` on a real VCF
file -- and every other step is a real HTTP request with a real session cookie.
The pipeline engine itself (`geper/main.py`) is NOT run: this proves the clinical
record's workflow, not the variant calling.

The two-organisation world, the seven principals per organisation and the login
helpers are w162's, imported rather than copied. Reads are made through domain
methods on an independent connection; the three places raw SQL stands in (the
release_events rows, the audit rows, the order row) are marked where used.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND
from clinical.data_access import PORTAL_RELEASE_CONSUMER
from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_record_pipeline_result import RUN_DOCUMENT, VCF_TEXT
from clinical.tests.test_w162_registration_and_consent import (
    MALFORMED,
    NOWHERE,
    STAFF_COOKIE,
    _staff_cookie,
)

# Fixtures shared from test_w162_registration_and_consent. They are BOUND AS NAMES rather than imported:
# pytest discovers a fixture by the attribute it finds in this module, so the
# assignment registers it exactly as an import would, while ruff (F811) does not
# mistake each test's use of the fixture as a parameter for a redefinition of an
# unused import. Plain constants and helpers are imported normally above.
client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world

PATIENT_COOKIE = "clinical_patient_session"
ALL = ("orderer", "labtech", "admin", "auditor", "interpreter", "approver", "nobody")
NOT_INTERPRETER = tuple(r for r in ALL if r != "interpreter")
NOT_APPROVER = tuple(r for r in ALL if r != "approver")


def _c(cookie):
    return {STAFF_COOKIE: cookie} if cookie is not None else {}


def _cookie(client, world, who, tag="a"):
    return _c(_staff_cookie(client, world[tag]["org_id"], world[tag][f"{who}_email"]))


def _post(client, cookies, path, body=None):
    return client.post(path, json=body, cookies=cookies) if body is not None else client.post(path, cookies=cookies)


# ─── driving the workflow ────────────────────────────────────────────────────


def http_to_qc_passed(client, world, tag="a", qc="passed"):
    """Catalogue entry -> order -> placement -> sample receipt -> QC verdict,
    every step over HTTP by the role that owns it."""
    admin = _cookie(client, world, "admin", tag)
    # A fresh catalogue name per call: two ACTIVE tests of one name in one
    # organisation are refused by migration 0008, correctly.
    created = _post(
        client,
        admin,
        "/admin/tests",
        {"name": f"WGS {tag} {uuid.uuid4().hex[:8]}", "assembly": "hg38", "genes": ["BRCA1"]},
    )
    assert created.status_code == 201, created.text
    test_id = created.json()["test_id"]

    orderer = _cookie(client, world, "orderer", tag)
    order = _post(
        client,
        orderer,
        "/clinic/orders",
        {
            "patient_id": str(world[tag]["patient"]),
            "test_id": test_id,
            "consent_id": str(world[tag]["consent"]),
            "required_scope": "testing",
        },
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["order_id"]
    assert _post(client, orderer, f"/clinic/orders/{order_id}/placement").status_code == 204

    labtech = _cookie(client, world, "labtech", tag)
    sample = _post(client, labtech, f"/lab/orders/{order_id}/samples", {"sample_type": "blood"})
    assert sample.status_code == 201, sample.text
    sample_id = sample.json()["sample_id"]
    verdict = _post(client, labtech, f"/lab/samples/{sample_id}/qc", {"qc_status": qc})
    assert verdict.status_code in (200, 204), verdict.text
    return {"order_id": order_id, "sample_id": sample_id, "test_id": test_id}


def pipeline_step(reader, world, tmp_path, flow, tag="a", name="run.vcf"):
    """What the background worker does once QC has passed: a System session for
    the organisation and ONE call to `record_pipeline_result`, on a real file
    whose hash was taken before the run. NOT an HTTP request, by design."""
    path = tmp_path / f"{tag}-{name}"
    path.write_text(VCF_TEXT, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    system = reader._create_system_session(world[tag]["org_id"])
    recorded = reader.record_pipeline_result(system, uuid.UUID(flow["sample_id"]), str(path), digest, RUN_DOCUMENT)
    return {**flow, "report_id": str(recorded.report_id), "interpretation_id": str(recorded.interpretation_id)}


def draft_report(client, world, reader, tmp_path, tag="a"):
    return pipeline_step(reader, world, tmp_path, http_to_qc_passed(client, world, tag), tag)


def concur(client, world, flow, tag="a", reason="I read the draft and concur"):
    return _post(
        client,
        _cookie(client, world, "interpreter", tag),
        f"/review/reports/{flow['report_id']}/concurrence",
        {"interpretation_id": flow["interpretation_id"], "reason": reason},
    )


def under_review(client, world, reader, tmp_path, tag="a"):
    flow = draft_report(client, world, reader, tmp_path, tag)
    assert concur(client, world, flow, tag).status_code == 201
    submitted = _post(
        client, _cookie(client, world, "interpreter", tag), f"/review/reports/{flow['report_id']}/submission"
    )
    assert submitted.status_code == 204, submitted.text
    return flow


def approved(client, world, reader, tmp_path, tag="a"):
    flow = under_review(client, world, reader, tmp_path, tag)
    done = _post(client, _cookie(client, world, "approver", tag), f"/review/reports/{flow['report_id']}/approval")
    assert done.status_code == 204, done.text
    return flow


def state_of(reader, world, flow, tag="a"):
    return reader.get_report(world[tag]["admin"], uuid.UUID(flow["report_id"]))["state"]


def _rows(sql, params=()):
    import os

    import psycopg

    with psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10) as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _sealed(report_id):
    """(state, approver_id, content_hash). `get_report` returns the state only,
    and no domain reader exposes the approval seal, so this one narrow SELECT
    stands in -- the columns this file asserts on and nothing else."""
    return _rows("SELECT state, approver_id, content_hash FROM reports WHERE id = %s", (report_id,))[0]


# ══════════════════════════════════════════════════════════════════════════════
# THE COMPLETE WORKFLOW
# ══════════════════════════════════════════════════════════════════════════════


class TestTheCompleteWorkflow:
    def test_order_to_a_report_in_the_patients_hands(self, client, world, reader, tmp_path):
        a = world["a"]
        flow = http_to_qc_passed(client, world)

        # QC passed: the worker's own gate says the order may be submitted.
        system = reader._create_system_session(a["org_id"])
        assert reader.check_qc_passed(system, uuid.UUID(flow["order_id"])) == "qc_ok"

        flow = pipeline_step(reader, world, tmp_path, flow)
        report = uuid.UUID(flow["report_id"])
        assert state_of(reader, world, flow) == "draft"

        # The patient cannot see a draft. Issue the patient a login and look.
        enrol = client.post(
            f"/admin/patients/{a['patient']}/logins",
            json={"email": "patient@portal.example", "password": "a patient password 1"},
            cookies=_cookie(client, world, "admin"),
        )
        assert enrol.status_code == 201, enrol.text
        opened = client.post(
            "/portal/login",
            json={"email": "patient@portal.example", "org_id": str(a["org_id"]), "password": "a patient password 1"},
        )
        assert opened.status_code == 204, opened.text
        patient = {PATIENT_COOKIE: opened.cookies[PATIENT_COOKIE]}
        assert [r["report_id"] for r in client.get("/portal/me/reports", cookies=patient).json()] == []

        # review: concurrence -> submission
        assert concur(client, world, flow).status_code == 201
        interpreter = _cookie(client, world, "interpreter")
        assert _post(client, interpreter, f"/review/reports/{report}/submission").status_code == 204
        assert state_of(reader, world, flow) == "under_review"
        assert client.get("/portal/me/reports", cookies=patient).json() == []

        # approval
        approver = _cookie(client, world, "approver")
        assert _post(client, approver, f"/review/reports/{report}/approval").status_code == 204
        state, approver_id, sealed_hash = _sealed(report)
        assert state == "approved"
        assert approver_id == a["approver_id"]
        assert sealed_hash
        assert reader.verify_report_integrity(a["approver"], report) is True
        assert client.get("/portal/me/reports", cookies=patient).json() == []  # approved is not released

        # release
        released = _post(client, approver, f"/review/reports/{report}/release")
        assert released.status_code == 201, released.text
        assert state_of(reader, world, flow) == "released"

        # THE PATIENT RECEIVES THE REPORT
        mine = client.get("/portal/me/reports", cookies=patient)
        assert mine.status_code == 200, mine.text
        assert [r["report_id"] for r in mine.json()] == [str(report)]
        assert client.get(f"/portal/me/reports/{report}", cookies=patient).status_code == 200

        # release_events: one row, the releasing approver, the sealed hash.
        rows = _rows("SELECT consumer, released_by, content_hash FROM release_events WHERE report_id = %s", (report,))
        assert rows == [(PORTAL_RELEASE_CONSUMER, a["approver_id"], sealed_hash)]

    def test_every_step_is_audited_with_the_acting_user(self, client, world, reader, tmp_path):
        a = world["a"]
        flow = approved(client, world, reader, tmp_path)
        _post(client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release")

        rows = _rows(
            "SELECT action, user_id, actor_role, org_id, outcome FROM audit_log "
            "WHERE action IN ('test_created','order_created','order_placed','sample_received','qc_recorded',"
            "'pipeline_result_recorded','record_sole_signatory','report_submitted_for_review',"
            "'report_approved','report_released') AND outcome = 'success'"
        )
        by_action = {}
        for action, user_id, role, org_id, outcome in rows:
            by_action.setdefault(action, []).append((user_id, role, org_id))
        actor = {
            "test_created": a["admin_id"],
            "order_created": a["orderer_id"],
            "order_placed": a["orderer_id"],
            "sample_received": a["labtech_id"],
            "qc_recorded": a["labtech_id"],
            "record_sole_signatory": a["interpreter_id"],
            "report_submitted_for_review": a["interpreter_id"],
            "report_approved": a["approver_id"],
            "report_released": a["approver_id"],
        }
        for action, user in actor.items():
            assert by_action.get(action), f"no success audit row for {action}"
            assert all(u == user and o == a["org_id"] for u, _, o in by_action[action]), (action, by_action[action])
        # the worker's step is attributed to the System principal, not to a human.
        assert by_action["pipeline_result_recorded"], by_action.keys()
        assert all(u not in actor.values() for u, _, _ in by_action["pipeline_result_recorded"])

    def test_a_failed_qc_stops_the_order_before_any_interpretation_is_recorded(self, client, world, reader):
        flow = http_to_qc_passed(client, world, qc="failed")
        system = reader._create_system_session(world["a"]["org_id"])
        # The worker's gate, checked BEFORE it runs the pipeline.
        assert reader.check_qc_passed(system, uuid.UUID(flow["order_id"])) == "qc_failed"
        # ...and nothing downstream of QC exists for the order.
        assert _rows("SELECT count(*) FROM interpretations")[0][0] == 0
        assert _rows("SELECT count(*) FROM reports")[0][0] == 0


# ══════════════════════════════════════════════════════════════════════════════
# INVALID TRANSITIONS
# ══════════════════════════════════════════════════════════════════════════════


class TestOutOfOrderTransitionsAreRefused:
    def test_a_draft_cannot_be_approved_or_released(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        approver = _cookie(client, world, "approver")
        approve = _post(client, approver, f"/review/reports/{flow['report_id']}/approval")
        assert approve.status_code == 400 and "draft" in approve.json()["detail"]
        release = _post(client, approver, f"/review/reports/{flow['report_id']}/release")
        assert release.status_code == 400 and "draft" in release.json()["detail"]
        assert state_of(reader, world, flow) == "draft"
        assert _rows("SELECT count(*) FROM release_events")[0][0] == 0

    def test_a_draft_cannot_be_submitted_without_a_claim_of_its_own(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        response = _post(
            client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/submission"
        )
        assert response.status_code == 400 and "no reviewer claim" in response.json()["detail"]
        assert state_of(reader, world, flow) == "draft"

    def test_a_report_under_review_cannot_be_released_or_submitted_again(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        release = _post(client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release")
        assert release.status_code == 400 and "under_review" in release.json()["detail"]
        again = _post(client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/submission")
        assert again.status_code == 400 and "under_review" in again.json()["detail"]
        assert state_of(reader, world, flow) == "under_review"

    def test_an_approved_report_cannot_be_approved_again_or_submitted(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        approver = _cookie(client, world, "approver")
        assert _post(client, approver, f"/review/reports/{flow['report_id']}/approval").status_code == 400
        resubmit = _post(
            client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/submission"
        )
        assert resubmit.status_code == 400
        assert state_of(reader, world, flow) == "approved"

    def test_a_second_release_to_the_portal_is_refused_and_adds_no_row(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        approver = _cookie(client, world, "approver")
        assert _post(client, approver, f"/review/reports/{flow['report_id']}/release").status_code == 201
        again = _post(client, approver, f"/review/reports/{flow['report_id']}/release")
        assert again.status_code == 400 and "already been released" in again.json()["detail"]
        assert _rows("SELECT consumer FROM release_events") == [(PORTAL_RELEASE_CONSUMER,)]
        assert state_of(reader, world, flow) == "released"

    def test_the_recipient_and_the_releaser_cannot_be_named_by_the_caller(self, client, world, reader, tmp_path):
        a, b = world["a"], world["b"]
        flow = approved(client, world, reader, tmp_path)
        response = _post(
            client,
            _cookie(client, world, "approver"),
            f"/review/reports/{flow['report_id']}/release",
            {"consumer": "Someone Else", "released_by": str(b["approver_id"]), "org_id": str(b["org_id"])},
        )
        assert response.status_code == 201, response.text
        assert _rows("SELECT consumer, released_by, org_id FROM release_events") == [
            (PORTAL_RELEASE_CONSUMER, a["approver_id"], a["org_id"])
        ]


# ══════════════════════════════════════════════════════════════════════════════
# ROLE PERMISSIONS
# ══════════════════════════════════════════════════════════════════════════════


class TestOnlyTheRightRoleMayActAndTheRefusalIsUniform:
    @pytest.mark.parametrize("who", NOT_INTERPRETER)
    def test_only_the_interpreter_records_claims_or_submits(self, client, world, reader, tmp_path, who):
        flow = draft_report(client, world, reader, tmp_path)
        cookie = _cookie(client, world, who)
        base = f"/review/reports/{flow['report_id']}"
        responses = [
            _post(
                client, cookie, f"{base}/concurrence", {"interpretation_id": flow["interpretation_id"], "reason": "r"}
            ),
            _post(
                client,
                cookie,
                f"{base}/disagreement",
                {
                    "interpretation_id": flow["interpretation_id"],
                    "variant_key": "1:1000:A:T",
                    "new_classification": "Likely pathogenic",
                    "reason": "r",
                },
            ),
            _post(client, cookie, f"{base}/submission"),
        ]
        for response in responses:
            assert response.status_code == 403, (who, response.text)
            assert response.json()["detail"] == ADMIN_FORBIDDEN
        assert _rows("SELECT count(*) FROM reviewer_claims")[0][0] == 0
        assert state_of(reader, world, flow) == "draft"

    @pytest.mark.parametrize("who", NOT_APPROVER)
    def test_only_the_approver_approves_or_releases(self, client, world, reader, tmp_path, who):
        flow = under_review(client, world, reader, tmp_path)
        cookie = _cookie(client, world, who)
        base = f"/review/reports/{flow['report_id']}"
        for response in (
            _post(client, cookie, f"{base}/approval"),
            _post(client, cookie, f"{base}/release"),
        ):
            assert response.status_code == 403, (who, response.text)
            assert response.json()["detail"] == ADMIN_FORBIDDEN
        assert state_of(reader, world, flow) == "under_review"
        assert _rows("SELECT count(*) FROM release_events")[0][0] == 0

    def test_the_approver_cannot_stand_in_for_the_interpreter_nor_the_reverse(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        approver = _cookie(client, world, "approver")
        assert _post(client, approver, f"/review/reports/{flow['report_id']}/submission").status_code == 403
        flow2 = under_review(client, world, reader, tmp_path)
        interpreter = _cookie(client, world, "interpreter")
        assert _post(client, interpreter, f"/review/reports/{flow2['report_id']}/approval").status_code == 403

    def test_unauthenticated_requests_are_401(self, client, world):
        for path, body in (
            (f"/review/reports/{NOWHERE}/concurrence", {"interpretation_id": NOWHERE, "reason": "r"}),
            (
                f"/review/reports/{NOWHERE}/disagreement",
                {"interpretation_id": NOWHERE, "variant_key": "k", "new_classification": "c", "reason": "r"},
            ),
            (f"/review/reports/{NOWHERE}/submission", None),
            (f"/review/reports/{NOWHERE}/approval", None),
            (f"/review/reports/{NOWHERE}/release", None),
        ):
            assert _post(client, {}, path, body).status_code == 401, path

    def test_a_patient_session_is_not_a_staff_credential(self, client, world):
        a = world["a"]
        client.post(
            f"/admin/patients/{a['patient']}/logins",
            json={"email": "p@portal.example", "password": "a patient password 1"},
            cookies=_cookie(client, world, "admin"),
        )
        opened = client.post(
            "/portal/login",
            json={"email": "p@portal.example", "org_id": str(a["org_id"]), "password": "a patient password 1"},
        )
        patient_sid = opened.cookies[PATIENT_COOKIE]
        response = client.post(f"/review/reports/{NOWHERE}/approval", cookies={STAFF_COOKIE: patient_sid})
        assert response.status_code == 401

    def test_the_gate_runs_before_the_identifier_is_judged(self, client, world):
        wrong_role = _cookie(client, world, "labtech")
        for target in (NOWHERE, MALFORMED):
            for verb in ("approval", "submission"):
                response = _post(client, wrong_role, f"/review/reports/{target}/{verb}")
                assert response.status_code == 403 and response.json()["detail"] == ADMIN_FORBIDDEN
        right_role = _cookie(client, world, "approver")
        for target in (NOWHERE, MALFORMED):
            response = _post(client, right_role, f"/review/reports/{target}/approval")
            assert response.status_code == 404 and response.json()["detail"] == ADMIN_NOT_FOUND


# ══════════════════════════════════════════════════════════════════════════════
# TENANT ISOLATION
# ══════════════════════════════════════════════════════════════════════════════


class TestNothingCrossesAnOrganisation:
    def test_another_organisations_report_reads_as_nothing_on_every_route(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path, "a")
        b_interpreter, b_approver = _cookie(client, world, "interpreter", "b"), _cookie(client, world, "approver", "b")
        report = flow["report_id"]
        attempts = (
            (b_interpreter, "concurrence", {"interpretation_id": flow["interpretation_id"], "reason": "r"}),
            (b_interpreter, "submission", None),
            (b_approver, "approval", None),
            (b_approver, "release", None),
        )
        for cookie, verb, body in attempts:
            real = _post(client, cookie, f"/review/reports/{report}/{verb}", body)
            invented_body = {**body, "interpretation_id": NOWHERE} if body and "interpretation_id" in body else body
            invented = _post(client, cookie, f"/review/reports/{NOWHERE}/{verb}", invented_body)
            assert real.status_code == invented.status_code == 404, (verb, real.text)
            assert real.content == invented.content, verb
        assert state_of(reader, world, flow) == "under_review"
        assert _rows("SELECT count(*) FROM release_events")[0][0] == 0

    def test_another_organisations_approver_cannot_approve_or_release(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path, "a")
        b_approver = _cookie(client, world, "approver", "b")
        assert _post(client, b_approver, f"/review/reports/{flow['report_id']}/release").status_code == 404
        assert state_of(reader, world, flow) == "approved"

    def test_a_claim_cannot_attach_a_report_to_another_interpretation(self, client, world, reader, tmp_path):
        first = draft_report(client, world, reader, tmp_path, "a")
        second = draft_report(client, world, reader, tmp_path, "a")
        response = _post(
            client,
            _cookie(client, world, "interpreter"),
            f"/review/reports/{first['report_id']}/concurrence",
            {"interpretation_id": second["interpretation_id"], "reason": "r"},
        )
        assert response.status_code == 400 and "is not a report of interpretation" in response.json()["detail"]
        assert _rows("SELECT count(*) FROM reviewer_claims")[0][0] == 0

    def test_a_claim_cannot_use_another_organisations_interpretation(self, client, world, reader, tmp_path):
        a_flow = draft_report(client, world, reader, tmp_path, "a")
        b_flow = draft_report(client, world, reader, tmp_path, "b")
        response = _post(
            client,
            _cookie(client, world, "interpreter", "a"),
            f"/review/reports/{a_flow['report_id']}/concurrence",
            {"interpretation_id": b_flow["interpretation_id"], "reason": "r"},
        )
        invented = _post(
            client,
            _cookie(client, world, "interpreter", "a"),
            f"/review/reports/{a_flow['report_id']}/concurrence",
            {"interpretation_id": NOWHERE, "reason": "r"},
        )
        assert response.status_code == invented.status_code == 404
        assert response.content == invented.content

    def test_the_patient_of_another_organisation_never_sees_the_report(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path, "a")
        _post(client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release")
        b = world["b"]
        client.post(
            f"/admin/patients/{b['patient']}/logins",
            json={"email": "b@portal.example", "password": "b patient password 1"},
            cookies=_cookie(client, world, "admin", "b"),
        )
        opened = client.post(
            "/portal/login",
            json={"email": "b@portal.example", "org_id": str(b["org_id"]), "password": "b patient password 1"},
        )
        theirs = {PATIENT_COOKIE: opened.cookies[PATIENT_COOKIE]}
        assert client.get("/portal/me/reports", cookies=theirs).json() == []
        # Another organisation's released report reads exactly like one that
        # does not exist: same status, same bytes, and never the report.
        real = client.get(f"/portal/me/reports/{flow['report_id']}", cookies=theirs)
        invented = client.get(f"/portal/me/reports/{NOWHERE}", cookies=theirs)
        assert real.status_code == invented.status_code and real.status_code != 200
        assert real.content == invented.content


# ══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING AND THE CLAIMS
# ══════════════════════════════════════════════════════════════════════════════


class TestClaimsAndBodies:
    def test_a_blank_reason_is_refused_and_records_nothing(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        response = concur(client, world, flow, reason="   ")
        assert response.status_code == 400
        assert _rows("SELECT count(*) FROM reviewer_claims")[0][0] == 0

    def test_a_disagreement_is_recorded_beside_the_original_not_over_it(self, client, world, reader, tmp_path):
        a = world["a"]
        flow = draft_report(client, world, reader, tmp_path)
        response = _post(
            client,
            _cookie(client, world, "interpreter"),
            f"/review/reports/{flow['report_id']}/disagreement",
            {
                "interpretation_id": flow["interpretation_id"],
                "variant_key": "1:1000:A:T",
                "new_classification": "Likely pathogenic",
                "reason": "segregation evidence",
            },
        )
        assert response.status_code == 201, response.text
        claims = reader.get_reviewer_claims(a["interpreter"], uuid.UUID(flow["report_id"]))
        assert [(c["claim_type"], c["actor_id"]) for c in claims] == [("disagree", a["interpreter_id"])]
        # a disagreement is a claim, so it also satisfies the submit precondition
        submitted = _post(
            client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/submission"
        )
        assert submitted.status_code == 204

    def test_a_disagreement_must_name_a_variant_and_a_classification(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        cookie = _cookie(client, world, "interpreter")
        path = f"/review/reports/{flow['report_id']}/disagreement"
        base = {
            "interpretation_id": flow["interpretation_id"],
            "variant_key": "1:1000:A:T",
            "new_classification": "VUS",
            "reason": "r",
        }
        assert _post(client, cookie, path, {**base, "new_classification": " "}).status_code == 400
        assert _post(client, cookie, path, {**base, "reason": ""}).status_code == 400
        assert _rows("SELECT count(*) FROM reviewer_claims")[0][0] == 0

    def test_a_claim_body_naming_an_actor_organisation_or_state_is_refused(self, client, world, reader, tmp_path):
        b = world["b"]
        flow = draft_report(client, world, reader, tmp_path)
        interpreter = _cookie(client, world, "interpreter")
        for extra in ({"actor_id": str(b["interpreter_id"])}, {"org_id": str(b["org_id"])}, {"claim_type": "accept"}):
            body = {"interpretation_id": flow["interpretation_id"], "reason": "r", **extra}
            assert (
                _post(client, interpreter, f"/review/reports/{flow['report_id']}/concurrence", body).status_code == 422
            )
        assert _rows("SELECT count(*) FROM reviewer_claims")[0][0] == 0
        assert _rows("SELECT count(*) FROM release_events")[0][0] == 0

    def test_the_signatory_recorded_is_always_the_session_user(self, client, world, reader, tmp_path):
        a = world["a"]
        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow).status_code == 201
        claims = reader.get_reviewer_claims(a["interpreter"], uuid.UUID(flow["report_id"]))
        assert [(c["claim_type"], c["actor_id"]) for c in claims] == [("sole_signatory", a["interpreter_id"])]

    def test_a_failed_attempt_leaves_the_state_and_the_evidence_untouched(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        before = _sealed(flow["report_id"])
        wrong = _post(client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/approval")
        assert wrong.status_code == 403
        after = _sealed(flow["report_id"])
        assert after == before == ("under_review", None, None)


# ══════════════════════════════════════════════════════════════════════════════
# THE ROUTES THEMSELVES
# ══════════════════════════════════════════════════════════════════════════════


class TestTheReviewRoutesAreRegisteredOnceBehindTheStaffSession:
    PATHS = {
        "/review/reports/{report_id}/concurrence": ("POST",),
        "/review/reports/{report_id}/disagreement": ("POST",),
        "/review/reports/{report_id}/submission": ("POST",),
        "/review/reports/{report_id}/approval": ("POST",),
        "/review/reports/{report_id}/release": ("POST",),
    }

    def test_each_is_served_once_with_the_declared_method_and_the_staff_dependency(self):
        from clinical.app import app
        from clinical.endpoints import _get_session
        from clinical.review_endpoints import router
        from clinical.tests.test_w132_structural_guards import served_routes

        served = served_routes(app)
        for path, methods in self.PATHS.items():
            assert [e for e in served if e[0] == path] == [(path, methods)], path
        assert len(router.routes) == len(self.PATHS)
        for route in router.routes:
            assert _get_session in [d.call for d in route.dependant.dependencies], route.path

    def test_each_handler_makes_exactly_one_domain_call(self):
        import ast
        import pathlib

        from clinical import review_endpoints

        tree = ast.parse(pathlib.Path(review_endpoints.__file__).read_text(encoding="utf-8"))
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        assert len(handlers) == 5
        for handler in handlers:
            calls = [
                n
                for n in ast.walk(handler)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and ast.unparse(n.func.value) == "dao"
            ]
            assert len(calls) == 1, (handler.name, [ast.unparse(c.func) for c in calls])

    def test_no_request_model_carries_an_organisation_actor_or_state(self):
        from clinical import review_endpoints

        for model in (
            review_endpoints.ConcurrenceRequest,
            review_endpoints.DisagreementRequest,
        ):
            assert model.model_config.get("extra") == "forbid"
            assert not {f for f in model.model_fields if "org" in f or "actor" in f or f in {"state", "released_by"}}


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT: DENIED ATTEMPTS ARE RECORDED ONCE, SUCCESSES ARE NEVER DOUBLED
# ══════════════════════════════════════════════════════════════════════════════


def _audit(action, outcome):
    return _rows(
        "SELECT user_id, actor_role, org_id, resource_type, details FROM audit_log WHERE action = %s AND outcome = %s",
        (action, outcome),
    )


def _disagree(client, cookies, flow):
    return _post(
        client,
        cookies,
        f"/review/reports/{flow['report_id']}/disagreement",
        {
            "interpretation_id": flow["interpretation_id"],
            "variant_key": "1:1000:A:T",
            "new_classification": "Likely pathogenic",
            "reason": "this free text must never reach the audit row",
        },
    )


class TestDeniedAttemptsAreAuditedExactlyOnce:
    @pytest.mark.parametrize("who", NOT_INTERPRETER)
    def test_a_denied_disagreement_writes_one_denied_row_and_changes_nothing(
        self, client, world, reader, tmp_path, who
    ):
        a = world["a"]
        flow = draft_report(client, world, reader, tmp_path)
        before = (_sealed(flow["report_id"]), _rows("SELECT count(*) FROM reviewer_claims")[0][0])

        assert _disagree(client, _cookie(client, world, who), flow).status_code == 403

        denied = _audit("record_disagreement", "denied")
        assert len(denied) == 1, denied
        user_id, role, org_id, resource_type, details = denied[0]
        assert (user_id, org_id, resource_type) == (a[f"{who}_id"], a["org_id"], "reviewer_claim")
        assert details["report_id"] == flow["report_id"] and details["variant_key"] == "1:1000:A:T"
        assert "free text" not in str(details)  # the reason is clinical prose and is not logged
        assert _audit("record_disagreement", "success") == []
        # a denied attempt changes no clinical state
        assert (_sealed(flow["report_id"]), _rows("SELECT count(*) FROM reviewer_claims")[0][0]) == before

    def test_a_successful_disagreement_writes_exactly_one_success_row_and_no_denied_row(
        self, client, world, reader, tmp_path
    ):
        a = world["a"]
        flow = draft_report(client, world, reader, tmp_path)
        assert _disagree(client, _cookie(client, world, "interpreter"), flow).status_code == 201
        success = _audit("record_disagreement", "success")
        assert len(success) == 1 and success[0][0] == a["interpreter_id"], success
        assert _audit("record_disagreement", "denied") == []

    def test_a_successful_concurrence_writes_exactly_one_success_row(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow).status_code == 201
        assert len(_audit("record_sole_signatory", "success")) == 1
        assert _audit("record_sole_signatory", "denied") == []

    @pytest.mark.parametrize("who", NOT_INTERPRETER)
    def test_a_denied_concurrence_writes_one_denied_row(self, client, world, reader, tmp_path, who):
        flow = draft_report(client, world, reader, tmp_path)
        response = _post(
            client,
            _cookie(client, world, who),
            f"/review/reports/{flow['report_id']}/concurrence",
            {"interpretation_id": flow["interpretation_id"], "reason": "r"},
        )
        assert response.status_code == 403
        assert len(_audit("record_sole_signatory", "denied")) == 1
        assert _audit("record_sole_signatory", "success") == []

    @pytest.mark.parametrize("who", NOT_APPROVER)
    def test_a_denied_release_writes_one_denied_row_and_releases_nothing(self, client, world, reader, tmp_path, who):
        a = world["a"]
        flow = approved(client, world, reader, tmp_path)
        assert (
            _post(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}/release").status_code
            == 403
        )
        denied = _audit("report_released", "denied")
        assert len(denied) == 1 and denied[0][0] == a[f"{who}_id"], denied
        assert _audit("report_released", "success") == []
        assert _rows("SELECT count(*) FROM release_events")[0][0] == 0
        assert state_of(reader, world, flow) == "approved"

    def test_a_successful_release_writes_exactly_one_success_row_and_no_denied_row(
        self, client, world, reader, tmp_path
    ):
        a = world["a"]
        flow = approved(client, world, reader, tmp_path)
        assert (
            _post(
                client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release"
            ).status_code
            == 201
        )
        success = _audit("report_released", "success")
        assert len(success) == 1 and success[0][0] == a["approver_id"], success
        assert _audit("report_released", "denied") == []

    @pytest.mark.parametrize("who", NOT_APPROVER)
    def test_a_denied_approval_writes_one_denied_row(self, client, world, reader, tmp_path, who):
        flow = under_review(client, world, reader, tmp_path)
        assert (
            _post(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}/approval").status_code
            == 403
        )
        assert len(_audit("report_approved", "denied")) == 1
        assert _audit("report_approved", "success") == []

    def test_an_already_released_refusal_is_not_a_second_release_event(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        approver = _cookie(client, world, "approver")
        _post(client, approver, f"/review/reports/{flow['report_id']}/release")
        assert _post(client, approver, f"/review/reports/{flow['report_id']}/release").status_code == 400
        assert len(_audit("report_released", "success")) == 1
        assert _audit("report_released", "denied") == []  # a state refusal is not an authorisation denial


# ── the decorator itself, with no database ───────────────────────────────────


class TestTheDenialsOnlyDecoratorDoesNotDuplicate:
    """`audit_denials_only` exercised on a stub, so the no-duplicate property is
    proved directly and not only through the HTTP surface."""

    @staticmethod
    def _stub():
        from clinical.data_access import AuthorizationError, Session, auditable

        class Connection:
            def commit(self):
                pass

        class Stub:
            def __init__(self):
                self.rows = []
                self._DataAccess__connection = Connection()

            def _write_audit_entry(self, action, resource_type, resource_id, outcome, **kw):
                self.rows.append((action, outcome))

            def _get_actor_role(self, session):
                return "Tester"

            @auditable(action="inner", resource_type="thing", resource_id_param="thing_id")
            def inner(self, session, thing_id, deny=False, fail=False):
                if deny:
                    raise AuthorizationError("inner refuses")
                if fail:
                    raise RuntimeError("inner broke")
                return "ok"

            @auditable(action="outer", resource_type="thing", auditable=False, audit_denials_only=True, reason="test")
            def outer(self, session, thing_id, gate=False, **kw):
                if gate:
                    raise AuthorizationError("outer refuses")
                return self.inner(session, thing_id, **kw)

        return Stub(), Session(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    def test_success_is_recorded_once_by_the_delegate_only(self):
        stub, session = self._stub()
        assert stub.outer(session, "t1") == "ok"
        assert stub.rows == [("inner", "success")]

    def test_the_wrappers_own_refusal_is_recorded_once(self):
        from clinical.data_access import AuthorizationError

        stub, session = self._stub()
        with pytest.raises(AuthorizationError):
            stub.outer(session, "t1", gate=True)
        assert stub.rows == [("outer", "denied")]

    def test_a_refusal_the_delegate_already_recorded_is_not_recorded_again(self):
        from clinical.data_access import AuthorizationError

        stub, session = self._stub()
        with pytest.raises(AuthorizationError):
            stub.outer(session, "t1", deny=True)
        assert stub.rows == [("inner", "denied")]

    def test_an_ordinary_error_is_recorded_once_by_the_delegate(self):
        stub, session = self._stub()
        with pytest.raises(RuntimeError):
            stub.outer(session, "t1", fail=True)
        assert stub.rows == [("inner", "error")]


# ══════════════════════════════════════════════════════════════════════════════
# CONCURRENT RELEASES: THE DATABASE, NOT THE READ-THEN-INSERT, IS THE CONTROL
# ══════════════════════════════════════════════════════════════════════════════


def _race(world, report_id, threads, attempt):
    """Run `attempt(dao, session)` on `threads` independent connections, all
    released from a barrier at once. Returns the list of outcomes."""
    import os
    import threading

    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    barrier = threading.Barrier(threads)
    outcomes = []
    lock = threading.Lock()

    def worker():
        with psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10) as connection:
            dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
            barrier.wait()
            try:
                result = attempt(dao, world["a"]["approver"])
                outcome = ("ok", result)
            except ValueError as exc:
                outcome = ("refused", str(exc))
            except Exception as exc:  # noqa: BLE001 -- anything else is the bug this hunts
                outcome = ("UNEXPECTED", f"{type(exc).__name__}: {exc}")
            with lock:
                outcomes.append(outcome)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    [t.start() for t in pool]
    [t.join(60) for t in pool]
    return outcomes


class TestSimultaneousReleasesCannotDuplicate:
    THREADS = 8

    def test_n_simultaneous_portal_releases_produce_exactly_one_event(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])

        outcomes = _race(world, report, self.THREADS, lambda dao, session: dao.release_report(session, report))

        assert sorted(kind for kind, _ in outcomes) == ["ok"] + ["refused"] * (self.THREADS - 1), outcomes
        assert all("already been released" in detail for kind, detail in outcomes if kind == "refused"), outcomes
        assert _rows("SELECT consumer FROM release_events WHERE report_id = %s", (report,)) == [
            (PORTAL_RELEASE_CONSUMER,)
        ]
        assert len(_audit("report_released", "success")) == 1
        assert state_of(reader, world, flow) == "released"

    def test_the_database_refuses_a_second_live_portal_event_whatever_the_application_does(
        self, client, world, reader, tmp_path
    ):
        """The constraint on its own, with the application's read-then-insert
        out of the picture: a raw second INSERT is refused by PostgreSQL."""
        import os

        import psycopg

        a = world["a"]
        flow = approved(client, world, reader, tmp_path)
        assert (
            _post(
                client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release"
            ).status_code
            == 201
        )
        with psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10) as connection:
            with pytest.raises(psycopg.errors.UniqueViolation) as info:
                connection.execute(
                    "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
                    "VALUES (%s, gen_random_uuid(), %s, %s, now(), %s, %s)",
                    (a["org_id"], flow["report_id"], PORTAL_RELEASE_CONSUMER, a["approver_id"], "0" * 64),
                )
            assert info.value.diag.constraint_name == "release_events_portal_once"

    def test_other_consumers_and_tombstoned_rows_are_not_constrained(self, client, world, reader, tmp_path):
        """The constraint is NARROW. The domain has always allowed one report
        to reach many consumers, and the same consumer twice (the retraction
        debt is 'one per distinct consumer' precisely because it happens)."""
        import os

        import psycopg

        a = world["a"]
        flow = approved(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        system = reader._create_system_session(a["org_id"])  # noqa: F841 -- documents that no human role is needed below
        with psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10) as connection:
            insert = (
                "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
                "VALUES (%s, gen_random_uuid(), %s, %s, now(), %s, %s)"
            )
            for consumer in ("Clinic A", "Clinic A", "LIMS"):  # same external consumer twice, and another
                connection.execute(insert, (a["org_id"], report, consumer, a["approver_id"], "0" * 64))
            # a portal event, then a tombstoned one beside it, then a live one
            connection.execute(insert, (a["org_id"], report, PORTAL_RELEASE_CONSUMER, a["approver_id"], "0" * 64))
            connection.execute(
                "UPDATE release_events SET tombstoned_at = now(), tombstoned_by = %s WHERE report_id = %s AND consumer = %s",
                (a["approver_id"], report, PORTAL_RELEASE_CONSUMER),
            )
            connection.execute(insert, (a["org_id"], report, PORTAL_RELEASE_CONSUMER, a["approver_id"], "0" * 64))
            connection.commit()
        assert _rows("SELECT count(*) FROM release_events WHERE report_id = %s", (report,))[0][0] == 5
