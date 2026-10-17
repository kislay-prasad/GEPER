"""
clinical/tests/test_w174_human_tool_read_api.py
───────────────────────────────────────────────

THE HUMAN-TOOL READ API. A clinician's sign-off tool, the report-revision refresh and the LIMS export's
status lookup used to learn a report's state and revision through `DataAccess`, i.e. through a database
connection string on the person's machine. That credential is the application's whole authority (the
database has no row-level security and the application role may write `sessions`), so it must not leave the
server network. Four read routes let those tools ask the service instead:

    GET /review/reports/{id}             state, interpretation, retraction flag
    GET /review/reports/{id}/revision    the authoritative revision block
    GET /auth/me                         who the session belongs to
    GET /auth/me/signing-identity        the caller's OWN printed identity

THE PROPERTIES PINNED, over real HTTP against a real PostgreSQL:
  * every route refuses an anonymous caller, a patient's session and a staff member without a reading role,
    and the refusal is the platform's ordinary one;
  * a report in another organisation reads exactly like one that does not exist (404, same body), and the
    role gate runs BEFORE any lookup, so a caller without a role learns nothing about existence either;
  * `revision: {}` on a 200 means the lookup SUCCEEDED and found nothing -- and nothing that FAILED ever
    looks like that: an unreadable record is a 500 carrying a code, a refusal is 403, an unknown report is
    404, a crash is a 500 with no `revision` at all;
  * the signing identity is the caller's own and cannot be pointed at a colleague.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND
from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import (
    MALFORMED,
    NOWHERE,
    STAFF_COOKIE,
    _staff_cookie,
)
from clinical.tests.test_w168_review_workflow import (
    ALL,
    _cookie,
    _post,
    _rows,
    approved,
    draft_report,
    state_of,
)

client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world

READERS = ("interpreter", "approver", "auditor", "admin")
NOT_READERS = tuple(r for r in ALL if r not in READERS)
PATIENT_COOKIE = "clinical_patient_session"
ROUTES = (
    "/review/reports/{rid}",
    "/review/reports/{rid}/revision",
)


def _get(client, cookies, path):
    return client.get(path, cookies=cookies)


def _identify(reader, world, *who, tag="a"):
    """A retraction names its author by printed identity (R10); give those staff one."""
    a = world[tag]
    for i, name in enumerate(who):
        reader.set_clinician_identity(
            a["admin"], a[f"{name}_id"], f"Dr. {name.title()}, MD", f"MCI-{i}0001", "ABC Diagnostics"
        )


def _patient_cookie(reader, world, client):
    from clinical.tests.test_w136_trends_retraction import _portal_login

    a = world["a"]
    reader.enrol_patient_login(a["admin"], a["patient"], "portal-patient@a.example", _w162.PASSWORD)
    return _portal_login(client, a["org_id"], "portal-patient@a.example")


def _exec(sql, params=()):
    import os

    import psycopg

    with psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10, autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql, params)


# ═════════════════════════════════════════════════════════════════════════════
# GET /review/reports/{id}
# ═════════════════════════════════════════════════════════════════════════════


class TestReportStatus:
    @pytest.mark.parametrize("who", READERS)
    def test_each_reading_role_gets_state_interpretation_and_the_retraction_flag(
        self, client, world, reader, tmp_path, who
    ):
        flow = draft_report(client, world, reader, tmp_path)
        response = _get(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report_id"] == flow["report_id"]
        assert body["interpretation_id"] == flow["interpretation_id"]
        assert body["state"] == "draft" and body["is_retracted"] is False
        assert set(body) == {"report_id", "interpretation_id", "state", "is_retracted", "created_at"}

    def test_the_state_follows_the_report_and_the_flag_follows_a_retraction(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        _identify(reader, world, "approver")
        cookie = _cookie(client, world, "auditor")
        url = f"/review/reports/{flow['report_id']}"
        assert _get(client, cookie, url).json()["state"] == "approved"
        assert _get(client, cookie, url).json()["is_retracted"] is False
        assert state_of(reader, world, flow) == "approved"
        retracted = _post(
            client,
            _cookie(client, world, "approver"),
            f"/admin/reports/{flow['report_id']}/retraction",
            {"reason": "wrong sample"},
        )
        assert retracted.status_code == 201, retracted.text
        after = _get(client, cookie, url).json()
        assert after["is_retracted"] is True and after["state"] == "approved"  # flagged, never hidden, never moved

    @pytest.mark.parametrize("who", NOT_READERS)
    def test_a_staff_member_without_a_reading_role_is_refused_uniformly(self, client, world, reader, tmp_path, who):
        flow = draft_report(client, world, reader, tmp_path)
        response = _get(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}")
        assert response.status_code == 403
        assert response.json() == {"detail": ADMIN_FORBIDDEN}

    def test_anonymous_and_patient_sessions_are_refused_like_every_other_staff_route(
        self, client, world, reader, tmp_path
    ):
        flow = draft_report(client, world, reader, tmp_path)
        anonymous = _get(client, {}, f"/review/reports/{flow['report_id']}")
        baseline = _get(client, {}, "/exceptions/worklist")
        assert anonymous.status_code == baseline.status_code == 401
        assert anonymous.json() == baseline.json()
        patient = _patient_cookie(reader, world, client)
        as_patient = _get(client, {PATIENT_COOKIE: patient}, f"/review/reports/{flow['report_id']}")
        assert as_patient.status_code == 401 and as_patient.json() == baseline.json()

    def test_a_report_of_another_organisation_reads_exactly_like_one_that_does_not_exist(
        self, client, world, reader, tmp_path
    ):
        flow = draft_report(client, world, reader, tmp_path, tag="a")
        foreign = _cookie(client, world, "interpreter", tag="b")
        cross = _get(client, foreign, f"/review/reports/{flow['report_id']}")
        nowhere = _get(client, foreign, f"/review/reports/{NOWHERE}")
        malformed = _get(client, foreign, f"/review/reports/{MALFORMED}")
        assert (
            (cross.status_code, cross.json())
            == (nowhere.status_code, nowhere.json())
            == (404, {"detail": ADMIN_NOT_FOUND})
        )
        assert (malformed.status_code, malformed.json()) == (404, {"detail": ADMIN_NOT_FOUND})

    def test_the_role_gate_runs_before_the_lookup_so_a_stranger_learns_nothing_about_existence(
        self, client, world, reader, tmp_path
    ):
        flow = draft_report(client, world, reader, tmp_path, tag="a")
        stranger_without_a_role = _cookie(client, world, "nobody", tag="b")
        real = _get(client, stranger_without_a_role, f"/review/reports/{flow['report_id']}")
        invented = _get(client, stranger_without_a_role, f"/review/reports/{NOWHERE}")
        assert (
            (real.status_code, real.json())
            == (invented.status_code, invented.json())
            == (403, {"detail": ADMIN_FORBIDDEN})
        )

    def test_the_organisation_is_the_sessions_and_a_query_string_cannot_change_it(
        self, client, world, reader, tmp_path
    ):
        flow = draft_report(client, world, reader, tmp_path, tag="a")
        foreign = _cookie(client, world, "interpreter", tag="b")
        response = client.get(f"/review/reports/{flow['report_id']}?org_id={world['a']['org_id']}", cookies=foreign)
        assert response.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# GET /review/reports/{id}/revision
# ═════════════════════════════════════════════════════════════════════════════


class TestReportRevision:
    @pytest.mark.parametrize("who", READERS)
    def test_a_successful_empty_lookup_is_an_explicit_empty_revision_on_a_200(
        self, client, world, reader, tmp_path, who
    ):
        flow = approved(client, world, reader, tmp_path)
        response = _get(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}/revision")
        assert response.status_code == 200, response.text
        assert response.json() == {"report_id": flow["report_id"], "revision": {}}

    def test_a_retraction_is_carried_with_its_facts(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        _identify(reader, world, "approver")
        _post(
            client,
            _cookie(client, world, "approver"),
            f"/admin/reports/{flow['report_id']}/retraction",
            {"reason": "wrong sample"},
        )
        body = _get(client, _cookie(client, world, "auditor"), f"/review/reports/{flow['report_id']}/revision").json()
        retracted = body["revision"]["retracted"]
        assert retracted["reason"] == "wrong sample" and retracted["issued_basis"] == "approved"
        assert {"issued_at", "retracted_at", "retracted_by"} <= set(retracted)
        assert "replacement_report_id" not in retracted

    def test_a_replacement_and_its_retention_state_are_carried(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        a = world["a"]
        _identify(reader, world, "approver")
        replacement = reader.create_report(a["interpreter"], uuid.UUID(flow["interpretation_id"]))
        reader.retract_report(
            a["approver"],
            uuid.UUID(flow["report_id"]),
            reason="superseded by a corrected report",
            replacement_report_id=replacement,
        )
        cookie = _cookie(client, world, "auditor")
        url = f"/review/reports/{flow['report_id']}/revision"
        retained = _get(client, cookie, url).json()["revision"]["retracted"]
        assert retained["replacement_report_id"] == str(replacement) and retained["replacement_retained"] is True
        _exec(
            "UPDATE reports SET tombstoned_at = now(), tombstoned_by = %s WHERE id = %s", (a["admin_id"], replacement)
        )
        gone = _get(client, cookie, url).json()["revision"]["retracted"]
        assert gone["replacement_retained"] is False

    @pytest.mark.parametrize("who", NOT_READERS)
    def test_a_staff_member_without_a_reading_role_is_refused_uniformly(self, client, world, reader, tmp_path, who):
        flow = approved(client, world, reader, tmp_path)
        response = _get(client, _cookie(client, world, who), f"/review/reports/{flow['report_id']}/revision")
        assert response.status_code == 403 and response.json() == {"detail": ADMIN_FORBIDDEN}
        assert "revision" not in response.json()

    def test_anonymous_is_refused_and_cross_organisation_is_not_found(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        url = f"/review/reports/{flow['report_id']}/revision"
        assert _get(client, {}, url).status_code == 401
        cross = _get(client, _cookie(client, world, "auditor", tag="b"), url)
        nowhere = _get(client, _cookie(client, world, "auditor", tag="b"), f"/review/reports/{NOWHERE}/revision")
        assert (
            (cross.status_code, cross.json())
            == (nowhere.status_code, nowhere.json())
            == (404, {"detail": ADMIN_NOT_FOUND})
        )
        assert "revision" not in cross.json()

    def test_a_retraction_whose_author_has_no_printed_identity_is_a_coded_500(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        _post(
            client,
            _cookie(client, world, "approver"),
            f"/admin/reports/{flow['report_id']}/retraction",
            {"reason": "wrong sample"},
        )
        response = _get(client, _cookie(client, world, "auditor"), f"/review/reports/{flow['report_id']}/revision")
        assert response.status_code == 500 and response.json()["code"] == "revision_record_unreadable"
        assert "revision" not in response.json()

    def test_an_unreadable_record_is_a_coded_500_and_never_an_empty_revision(self, client, world, reader, tmp_path):
        """A retraction that says it was made from 'released' but whose report was never released cannot be
        stated truthfully. The domain refuses (BrokenRevisionRecordError); the route says so explicitly."""
        flow = approved(client, world, reader, tmp_path)
        _identify(reader, world, "approver")
        _post(
            client,
            _cookie(client, world, "approver"),
            f"/admin/reports/{flow['report_id']}/retraction",
            {"reason": "wrong sample"},
        )
        _exec(
            "UPDATE report_retractions SET retracted_from_state = 'released' WHERE report_id = %s", (flow["report_id"],)
        )
        response = _get(client, _cookie(client, world, "auditor"), f"/review/reports/{flow['report_id']}/revision")
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "revision_record_unreadable" and body["detail"]
        assert "revision" not in body

    def test_a_crash_is_a_500_without_a_revision_and_not_an_empty_one(
        self, world, reader, tmp_path, clinical_dsn, monkeypatch
    ):
        from clinical.app import app
        from clinical.data_access import DataAccess
        from clinical.endpoints import _get_data_access

        flow = approved(TestClient(app), world, reader, tmp_path)

        class Exploding(DataAccess):
            def read_report_revision(self, session, report_id):
                raise RuntimeError("database fell over")

        def _exploding():
            import os

            import psycopg

            connection = psycopg.connect(os.environ["CLINICAL_DSN"], autocommit=False)
            try:
                yield Exploding(connection)
            finally:
                connection.close()

        app.dependency_overrides[_get_data_access] = _exploding
        try:
            crashing = TestClient(app, raise_server_exceptions=False)
            cookie = _staff_cookie(crashing, world["a"]["org_id"], world["a"]["auditor_email"])
            response = crashing.get(f"/review/reports/{flow['report_id']}/revision", cookies={STAFF_COOKIE: cookie})
        finally:
            app.dependency_overrides.pop(_get_data_access, None)
        assert response.status_code == 500
        assert "revision" not in response.text


# ═════════════════════════════════════════════════════════════════════════════
# GET /auth/me and GET /auth/me/signing-identity
# ═════════════════════════════════════════════════════════════════════════════


class TestCallerIdentity:
    def test_me_names_the_session_user_and_organisation_and_nothing_else(self, client, world):
        a = world["a"]
        response = _get(client, _cookie(client, world, "auditor"), "/auth/me")
        assert response.status_code == 200
        assert response.json() == {"user_id": str(a["auditor_id"]), "org_id": str(a["org_id"])}

    def test_me_is_per_session_not_per_query(self, client, world):
        a = world["a"]
        response = client.get(f"/auth/me?user_id={a['admin_id']}", cookies=_cookie(client, world, "auditor"))
        assert response.json()["user_id"] == str(a["auditor_id"])

    def test_me_refuses_anonymous_and_a_patient_session(self, client, world, reader):
        assert _get(client, {}, "/auth/me").status_code == 401
        patient = _patient_cookie(reader, world, client)
        assert _get(client, {PATIENT_COOKIE: patient}, "/auth/me").status_code == 401

    def test_an_incomplete_signing_identity_is_a_coded_409_naming_the_missing_fields(self, client, world):
        response = _get(client, _cookie(client, world, "approver"), "/auth/me/signing-identity")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "incomplete_clinician_identity"
        assert (
            "full_name" in body["detail"] and "registration_number" in body["detail"] and "hospital" in body["detail"]
        )

    def test_a_complete_signing_identity_is_the_callers_own(self, client, world, reader):
        a = world["a"]
        reader.set_clinician_identity(a["admin"], a["approver_id"], "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics")
        reader.set_clinician_identity(a["admin"], a["auditor_id"], "Dr. B. Auditor, MD", "MCI-22222", "XYZ Hospital")
        response = _get(client, _cookie(client, world, "approver"), "/auth/me/signing-identity")
        assert response.status_code == 200
        assert response.json() == {
            "user_id": str(a["approver_id"]),
            "full_name": "Dr. A. Sharma, MD",
            "registration_number": "MCI-12345",
            "hospital": "ABC Diagnostics",
        }

    def test_a_colleagues_identity_cannot_be_requested(self, client, world, reader):
        a = world["a"]
        reader.set_clinician_identity(a["admin"], a["auditor_id"], "Dr. B. Auditor, MD", "MCI-22222", "XYZ Hospital")
        reader.set_clinician_identity(a["admin"], a["approver_id"], "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics")
        response = client.get(
            f"/auth/me/signing-identity?user_id={a['auditor_id']}", cookies=_cookie(client, world, "approver")
        )
        assert response.status_code == 200 and response.json()["registration_number"] == "MCI-12345"
        assert "MCI-22222" not in response.text

    def test_anonymous_is_refused(self, client):
        assert _get(client, {}, "/auth/me/signing-identity").status_code == 401


class TestTheReadsWriteNothing:
    def test_no_audit_row_and_no_state_change_come_from_a_read(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        before = _rows("SELECT count(*) FROM audit_log WHERE resource_id = %s", (flow["report_id"],))
        cookie = _cookie(client, world, "auditor")
        for path in ROUTES:
            assert _get(client, cookie, path.format(rid=flow["report_id"])).status_code == 200
        after = _rows("SELECT count(*) FROM audit_log WHERE resource_id = %s", (flow["report_id"],))
        assert before == after
        assert state_of(reader, world, flow) == "approved"
