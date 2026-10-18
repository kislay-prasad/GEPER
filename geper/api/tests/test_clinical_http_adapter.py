"""
api/tests/test_clinical_http_adapter.py
───────────────────────────────────────

`review/clinical_http.py`: the clinical platform reached over its authenticated HTTPS API instead of a database
connection, for the tools a PERSON runs (revision refresh, LIMS status lookup).

TWO KINDS OF TEST
  * AGAINST THE REAL SERVICE: the real clinical FastAPI app, the real `DataAccess` and a real PostgreSQL
    (api/tests/conftest.py::clinical_conn), reached through the adapter over an in-process HTTPS client. PARITY
    tests ask the same question through the adapter and through `DataAccess` directly and require the same answer,
    so the adapter cannot drift from the domain.
  * AGAINST A STAND-IN SERVER (httpx.MockTransport): every status code and malformed body the real service would
    not normally produce, to pin the ERROR MAPPING -- so that no failure is ever read as an empty answer.

WHAT THE TOOLS ABOVE IT MUST DO, ALSO PINNED: refresh and the export's status lookup work with the database
unreachable and no DSN configured, and REFUSE (rather than falling back to a database connection) when no service
address is given.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from api.tests import test_report_revision_refresh as _r
from api.tests import test_signoff_clinical_link as _base
from clinical.data_access import (
    AuthenticationError,
    AuthorizationError,
    BrokenRevisionRecordError,
    IncompleteClinicianIdentityError,
    NotFoundError,
    SessionExpired,
)
from report import export_lims as lims
from report.clinical_report_builder import normalize_report_revision
from review import signoff as s
from review.clinical_http import ClinicalServiceError, HttpClinicalDataAccess
from review.revision_status import RevisionStatusError, fetch_report_revision_status
from utils.exceptions import LIMSExportBlockedError, SignoffError

dao = _base.dao
clinical = _base.clinical
run_dir = _base.run_dir
linked_run_dir = _base.linked_run_dir
creds = _base.creds
signed = _r.signed

URL = "https://clinical.test"


@pytest.fixture
def service(dao):
    """The real clinical app, answering from the test database through the test's DataAccess."""
    from clinical.app import app
    from clinical.auth_endpoints import _LOGIN_THROTTLE
    from clinical.endpoints import _get_data_access

    _LOGIN_THROTTLE.reset()

    def _shared():
        yield dao

    app.dependency_overrides[_get_data_access] = _shared
    http = TestClient(app, base_url=URL, raise_server_exceptions=False)
    try:
        yield http
    finally:
        app.dependency_overrides.pop(_get_data_access, None)
        _LOGIN_THROTTLE.reset()


def _adapter(service):
    return HttpClinicalDataAccess(client=service)


def _login(adapter, creds, clinical):
    return adapter.login(creds.email, clinical["org_id"], creds.password)


# ═════════════════════════════════════════════════════════════════════════════
# parity with DataAccess, against the real service
# ═════════════════════════════════════════════════════════════════════════════


class TestLoginAndIdentity:
    def test_login_returns_the_same_user_and_organisation_the_database_knows(self, service, clinical, creds):
        session = _login(_adapter(service), creds, clinical)
        assert session.user_id == clinical["signatory"].user_id and session.org_id == clinical["org_id"]

    def test_a_wrong_password_is_an_authentication_error_that_does_not_repeat_the_password(self, service, clinical):
        bad = _base.s.ClinicalCredentials(email="signatory@org-a.test", password="not-the-password")
        with pytest.raises(AuthenticationError) as caught:
            _adapter(service).login(bad.email, clinical["org_id"], bad.password)
        assert "not-the-password" not in str(caught.value)

    def test_another_organisation_cannot_be_logged_into_with_this_one_s_credentials(self, service, clinical, creds):
        with pytest.raises(AuthenticationError):
            _adapter(service).login(creds.email, uuid.uuid4(), creds.password)

    def test_the_signing_identity_is_the_callers_own_and_matches_the_database(self, service, clinical, creds, dao):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        over_http = adapter.get_signing_identity(session, session.user_id)
        direct = dao.get_signing_identity(clinical["signatory"], clinical["signatory"].user_id)
        assert over_http == direct

    def test_an_incomplete_identity_raises_what_the_database_path_raises(self, service, clinical):
        adapter = _adapter(service)
        session = adapter.login("unrecorded@org-a.test", clinical["org_id"], "password")
        with pytest.raises(IncompleteClinicianIdentityError):
            adapter.get_signing_identity(session, session.user_id)

    def test_a_colleagues_identity_cannot_be_requested_and_no_request_is_made(self, service, clinical, creds):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        with pytest.raises(AuthorizationError):
            adapter.get_signing_identity(session, clinical["admin"].user_id)

    def test_a_session_the_adapter_did_not_open_is_refused(self, service, clinical, creds):
        adapter = _adapter(service)
        _login(adapter, creds, clinical)
        with pytest.raises(ClinicalServiceError, match="not opened by this adapter"):
            adapter.get_report(clinical["signatory"], clinical["report_id"])


class TestReportAndRevisionParity:
    def _both(self, service, clinical, creds, dao):
        adapter = _adapter(service)
        return adapter, _login(adapter, creds, clinical)

    def test_get_report_matches_the_database(self, service, clinical, creds, dao):
        adapter, session = self._both(service, clinical, creds, dao)
        over_http = adapter.get_report(session, clinical["report_id"])
        direct = dao.get_report(clinical["signatory"], clinical["report_id"])
        for key in ("id", "interpretation_id", "state", "is_retracted"):
            assert over_http[key] == direct[key]
        assert over_http["created_at"] == direct["created_at"]

    def test_an_unknown_report_is_none_exactly_as_it_is_directly(self, service, clinical, creds, dao):
        adapter, session = self._both(service, clinical, creds, dao)
        assert adapter.get_report(session, uuid.uuid4()) is None
        assert dao.get_report(clinical["signatory"], uuid.uuid4()) is None

    def test_another_organisations_report_is_none_and_its_revision_is_not_found(
        self, service, clinical, creds, dao, clinical_conn
    ):
        other = _r._second_organisation(dao, clinical_conn)
        adapter, session = self._both(service, clinical, creds, dao)
        assert adapter.get_report(session, other["report_id"]) is None
        with pytest.raises(NotFoundError):
            adapter.get_report_revision(session, other["report_id"])

    def test_an_empty_revision_is_an_empty_mapping_and_equals_the_direct_answer(self, service, clinical, creds, dao):
        adapter, session = self._both(service, clinical, creds, dao)
        assert adapter.get_report_revision(session, clinical["report_id"]) == {}
        assert dao.get_report_revision(clinical["signatory"], clinical["report_id"]) == {}

    @pytest.mark.parametrize("replacement", ["none", "retained", "gone"])
    def test_a_retraction_normalises_to_the_same_revision_either_way(
        self, signed, service, clinical, creds, dao, clinical_conn, replacement
    ):
        replacement_id = None
        if replacement != "none":
            replacement_id = _r._draft_report(clinical, dao)
        _r._retract(clinical, dao, replacement_id)
        if replacement == "gone":
            _r._tombstone(clinical_conn, replacement_id, clinical["signatory"].user_id)
        adapter, session = self._both(service, clinical, creds, dao)
        via_http = normalize_report_revision(adapter.get_report_revision(session, clinical["report_id"]))
        direct = normalize_report_revision(dao.get_report_revision(clinical["signatory"], clinical["report_id"]))
        assert via_http == direct and via_http["is_retracted"] is True
        if replacement != "none":
            assert via_http["retracted"]["replacement_retained"] is (replacement == "retained")

    def test_the_status_validation_gives_the_same_status_through_both_paths(
        self, signed, service, clinical, creds, dao
    ):
        _r._retract(clinical, dao)
        adapter, session = self._both(service, clinical, creds, dao)
        kwargs = dict(
            org_id=clinical["org_id"], interpretation_id=clinical["interpretation_id"], report_id=clinical["report_id"]
        )
        http_status = fetch_report_revision_status(adapter, session, **kwargs)
        direct_status = fetch_report_revision_status(dao, clinical["signatory"], **kwargs)
        assert http_status.revision == direct_status.revision
        assert (http_status.report_state, http_status.is_retracted) == (
            direct_status.report_state,
            direct_status.is_retracted,
        )

    def test_a_link_naming_another_interpretation_is_refused_through_the_adapter_too(
        self, service, clinical, creds, dao
    ):
        adapter, session = self._both(service, clinical, creds, dao)
        with pytest.raises(RevisionStatusError, match="disagree about which report"):
            fetch_report_revision_status(
                adapter,
                session,
                org_id=clinical["org_id"],
                interpretation_id=uuid.uuid4(),
                report_id=clinical["report_id"],
            )


class TestAuthorizationAndFailuresAreNeverEmptyAnswers:
    def test_an_account_without_a_reading_role_is_refused(self, service, clinical, dao, clinical_conn):
        _base._login_with_roles(
            dao, clinical_conn, clinical["org_id"], clinical["admin"], "orderer@org-a.test", ("Orderer",)
        )
        adapter = _adapter(service)
        session = adapter.login("orderer@org-a.test", clinical["org_id"], "password")
        with pytest.raises(AuthorizationError):
            adapter.get_report(session, clinical["report_id"])
        with pytest.raises(AuthorizationError):
            adapter.get_report_revision(session, clinical["report_id"])

    def test_an_unreadable_revision_record_is_the_domains_own_exception(
        self, service, clinical, creds, dao, monkeypatch
    ):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)

        def broken(session, report_id):
            raise BrokenRevisionRecordError("a retraction with no usable reason")

        monkeypatch.setattr(dao, "get_report_revision", broken)
        with pytest.raises(BrokenRevisionRecordError, match="no usable reason"):
            adapter.get_report_revision(session, clinical["report_id"])

    def test_a_server_crash_is_a_service_error_and_never_an_empty_revision(
        self, service, clinical, creds, dao, monkeypatch
    ):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)

        def explode(session, report_id):
            raise RuntimeError("database fell over")

        monkeypatch.setattr(dao, "get_report_revision", explode)
        with pytest.raises(ClinicalServiceError):
            adapter.get_report_revision(session, clinical["report_id"])

    def test_an_ended_session_is_a_session_expired_not_an_empty_answer(self, service, clinical, creds):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        service.cookies.clear()  # the server no longer recognises the caller
        with pytest.raises(SessionExpired):
            adapter.get_report_revision(session, clinical["report_id"])

    def test_logout_ends_the_session_on_the_server(self, service, clinical, creds):
        adapter = _adapter(service)
        _login(adapter, creds, clinical)
        stolen = dict(service.cookies)
        adapter.logout()
        replay = TestClient(service.app, base_url=URL, cookies=stolen)
        assert replay.get("/auth/me").status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# error mapping, against a stand-in server
# ═════════════════════════════════════════════════════════════════════════════


def _stub(handler):
    return HttpClinicalDataAccess(client=httpx.Client(base_url=URL, transport=httpx.MockTransport(handler)))


def _logged_in(handler):
    """An adapter whose login succeeds and whose other answers come from `handler`."""
    user = uuid.uuid4()
    org = uuid.uuid4()

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(204)
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"user_id": str(user), "org_id": str(org)})
        return handler(request)

    adapter = _stub(route)
    return adapter, adapter.login("a@b.test", org, "pw"), org


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status, exception",
        [
            (401, SessionExpired),
            (403, AuthorizationError),
            (404, NotFoundError),
            (502, ClinicalServiceError),
            (503, ClinicalServiceError),
        ],
    )
    def test_revision_statuses(self, status, exception):
        adapter, session, _org = _logged_in(lambda request: httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(exception):
            adapter.get_report_revision(session, uuid.uuid4())

    def test_a_plain_500_is_a_service_error_and_a_coded_500_is_the_domains_exception(self):
        plain, session, _ = _logged_in(lambda r: httpx.Response(500, text="boom"))
        with pytest.raises(ClinicalServiceError):
            plain.get_report_revision(session, uuid.uuid4())
        coded, session, _ = _logged_in(
            lambda r: httpx.Response(500, json={"detail": "unreadable because X", "code": "revision_record_unreadable"})
        )
        with pytest.raises(BrokenRevisionRecordError, match="unreadable because X"):
            coded.get_report_revision(session, uuid.uuid4())

    @pytest.mark.parametrize(
        "body",
        [
            {"revision": []},
            {"revision": None},
            {"revision": "{}"},
            {},
            {"report_id": "someone-else", "revision": {}},
            [],
            "text",
        ],
    )
    def test_a_200_that_is_not_a_mapping_naming_the_same_report_is_not_an_answer(self, body):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ClinicalServiceError):
            adapter.get_report_revision(session, report)

    def test_a_200_with_a_matching_empty_mapping_is_the_one_successful_empty_answer(self):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json={"report_id": str(report), "revision": {}}))
        assert adapter.get_report_revision(session, report) == {}

    def test_a_body_that_is_not_json_is_a_service_error(self):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, text="<html>"))
        with pytest.raises(ClinicalServiceError):
            adapter.get_report_revision(session, uuid.uuid4())

    def test_report_statuses(self):
        for status, expected in ((404, None),):
            adapter, session, _ = _logged_in(lambda r, st=status: httpx.Response(st, json={"detail": "x"}))
            assert adapter.get_report(session, uuid.uuid4()) is expected
        for status, exception in ((401, SessionExpired), (403, AuthorizationError), (500, ClinicalServiceError)):
            adapter, session, _ = _logged_in(lambda r, st=status: httpx.Response(st, json={"detail": "x"}))
            with pytest.raises(exception):
                adapter.get_report(session, uuid.uuid4())

    def test_a_malformed_report_body_is_a_service_error(self):
        for body in (
            {},
            {"report_id": "x"},
            {
                "report_id": str(uuid.uuid4()),
                "interpretation_id": str(uuid.uuid4()),
                "state": "draft",
                "is_retracted": "no",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ):
            adapter, session, _ = _logged_in(lambda r, b=body: httpx.Response(200, json=b))
            with pytest.raises(ClinicalServiceError):
                adapter.get_report(session, uuid.uuid4())

    def test_login_statuses(self):
        for status, exception in (
            (401, AuthenticationError),
            (429, AuthenticationError),
            (500, ClinicalServiceError),
            (200, ClinicalServiceError),
        ):
            adapter = _stub(lambda r, st=status: httpx.Response(st, json={"detail": "x"}))
            with pytest.raises(exception):
                adapter.login("a@b.test", uuid.uuid4(), "pw")

    def test_login_refuses_a_service_that_reports_a_different_organisation(self):
        adapter = _stub(
            lambda r: (
                httpx.Response(204)
                if r.url.path == "/auth/login"
                else httpx.Response(200, json={"user_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4())})
            )
        )
        with pytest.raises(ClinicalServiceError, match="different organisation"):
            adapter.login("a@b.test", uuid.uuid4(), "pw")

    def test_an_unreachable_service_is_a_service_error_without_the_request_body(self):
        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        adapter = _stub(refuse)
        with pytest.raises(ClinicalServiceError) as caught:
            adapter.login("a@b.test", uuid.uuid4(), "secret-password-value")
        assert "secret-password-value" not in str(caught.value) and "ConnectError" in str(caught.value)

    def test_identity_statuses(self):
        adapter, session, _ = _logged_in(
            lambda r: httpx.Response(409, json={"detail": "missing hospital", "code": "incomplete_clinician_identity"})
        )
        with pytest.raises(IncompleteClinicianIdentityError, match="missing hospital"):
            adapter.get_signing_identity(session, session.user_id)
        other, session, _ = _logged_in(lambda r: httpx.Response(409, json={"detail": "x"}))
        with pytest.raises(ClinicalServiceError):
            other.get_signing_identity(session, session.user_id)

    def test_redirects_are_not_followed(self):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(302, headers={"location": "https://evil.test/"}))
        with pytest.raises(ClinicalServiceError):
            adapter.get_report(session, uuid.uuid4())


def _signoff_body(report, **over):
    body = {
        "report_id": str(report),
        "interpretation_id": str(uuid.uuid4()),
        "state": "draft",
        "is_retracted": False,
        "approver_id": None,
        "approved_at": None,
        "has_content_hash": False,
        "claims": [],
    }
    body.update(over)
    return body


class TestSignoffStateReadOnly:
    """`get_signoff_state`: where a report's sign-off stands. Read-only; claims come back without their reasons."""

    def _both(self, service, clinical, creds, dao, report=None):
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        report = report or clinical["report_id"]
        over_http = adapter.get_signoff_state(session, report)
        direct = dao.read_signoff_state(clinical["signatory"], report)
        return over_http, direct

    @staticmethod
    def _same(over_http, direct):
        import datetime

        assert over_http["id"] == direct["id"] and over_http["interpretation_id"] == direct["interpretation_id"]
        assert over_http["state"] == direct["state"] and over_http["is_retracted"] == direct["is_retracted"]
        assert over_http["approver_id"] == direct["approver_id"]
        assert over_http["has_content_hash"] == direct["has_content_hash"]
        assert over_http["approved_at"] == direct["approved_at"]
        assert [(c["id"], c["claim_type"], c["actor_id"]) for c in over_http["claims"]] == [
            (c["id"], c["claim_type"], c["actor_id"]) for c in direct["claims"]
        ]
        for http_claim, direct_claim in zip(over_http["claims"], direct["claims"]):
            assert http_claim["recorded_at"] == datetime.datetime.fromisoformat(direct_claim["recorded_at"])

    def test_a_draft_with_no_claim_matches_the_database(self, service, clinical, creds, dao):
        over_http, direct = self._both(service, clinical, creds, dao)
        self._same(over_http, direct)
        assert over_http["state"] == "draft" and over_http["claims"] == []

    def test_a_recorded_claim_and_each_later_state_match_the_database(self, service, clinical, creds, dao):
        signatory = clinical["signatory"]
        dao.record_sole_signatory_claim(signatory, clinical["interpretation_id"], clinical["report_id"], "the reason")
        claim_only, direct = self._both(service, clinical, creds, dao)
        self._same(claim_only, direct)
        assert claim_only["state"] == "draft" and [c["claim_type"] for c in claim_only["claims"]] == ["sole_signatory"]
        assert claim_only["claims"][0]["actor_id"] == signatory.user_id

        dao.submit_for_review(signatory, clinical["report_id"])
        under_review, direct = self._both(service, clinical, creds, dao)
        self._same(under_review, direct)
        assert under_review["state"] == "under_review" and under_review["approver_id"] is None

        dao.approve_report(signatory, clinical["report_id"])
        approved, direct = self._both(service, clinical, creds, dao)
        self._same(approved, direct)
        assert approved["state"] == "approved" and approved["approver_id"] == signatory.user_id
        assert approved["approved_at"] is not None and approved["has_content_hash"] is True

    def test_a_retraction_is_flagged_and_the_state_is_unchanged(self, signed, service, clinical, creds, dao):
        _r._retract(clinical, dao)
        over_http, direct = self._both(service, clinical, creds, dao)
        self._same(over_http, direct)
        assert over_http["is_retracted"] is True and over_http["state"] == "approved"

    def test_no_reason_or_clinical_text_is_exposed_in_the_result(self, service, clinical, creds, dao):
        dao.record_sole_signatory_claim(
            clinical["signatory"], clinical["interpretation_id"], clinical["report_id"], "PRIVATE-REASON-TEXT"
        )
        over_http, _direct = self._both(service, clinical, creds, dao)
        assert set(over_http["claims"][0]) == {"id", "claim_type", "actor_id", "recorded_at"}
        assert "PRIVATE-REASON-TEXT" not in repr(over_http)

    def test_an_unknown_report_and_another_organisations_report_are_none(
        self, service, clinical, creds, dao, clinical_conn
    ):
        other = _r._second_organisation(dao, clinical_conn)
        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        assert adapter.get_signoff_state(session, uuid.uuid4()) is None
        assert adapter.get_signoff_state(session, other["report_id"]) is None

    def test_a_caller_without_a_reading_role_is_refused(self, service, clinical, dao, clinical_conn):
        _base._login_with_roles(
            dao, clinical_conn, clinical["org_id"], clinical["admin"], "orderer@org-a.test", ("Orderer",)
        )
        adapter = _adapter(service)
        session = adapter.login("orderer@org-a.test", clinical["org_id"], "password")
        with pytest.raises(AuthorizationError):
            adapter.get_signoff_state(session, clinical["report_id"])

    def test_it_changes_nothing_in_the_database(self, service, clinical, creds, dao, clinical_conn):
        dao.record_sole_signatory_claim(
            clinical["signatory"], clinical["interpretation_id"], clinical["report_id"], "r"
        )

        def snapshot():
            return (
                _base._report_row(clinical_conn, clinical["report_id"]),
                _base._claims(clinical_conn, clinical["report_id"]),
            )

        adapter = _adapter(service)
        session = _login(adapter, creds, clinical)
        before = snapshot()
        for _ in range(3):
            adapter.get_signoff_state(session, clinical["report_id"])
        assert snapshot() == before

    def test_a_session_the_adapter_did_not_open_is_refused(self, service, clinical, creds):
        adapter = _adapter(service)
        _login(adapter, creds, clinical)
        with pytest.raises(ClinicalServiceError, match="not opened by this adapter"):
            adapter.get_signoff_state(clinical["signatory"], clinical["report_id"])


class TestSignoffStateAgainstAStandIn:
    def test_it_makes_exactly_one_get_and_no_other_request(self):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=_signoff_body(report)))
        seen = []
        original = adapter._client.request

        def spy(method, path, **kwargs):
            seen.append((method, path, kwargs.get("json"), kwargs.get("content")))
            return original(method, path, **kwargs)

        adapter._client.request = spy
        adapter.get_signoff_state(session, report)
        assert seen == [("GET", f"/review/reports/{report}/signoff-state", None, None)]

    @pytest.mark.parametrize(
        "status, exception",
        [(401, SessionExpired), (403, AuthorizationError), (500, ClinicalServiceError), (502, ClinicalServiceError)],
    )
    def test_statuses(self, status, exception):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(exception):
            adapter.get_signoff_state(session, uuid.uuid4())

    def test_a_404_is_none_and_not_an_error(self):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(404, json={"detail": "x"}))
        assert adapter.get_signoff_state(session, uuid.uuid4()) is None

    def test_a_well_formed_answer_with_an_approval_and_claims_is_parsed(self):
        report, actor, claim = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        body = _signoff_body(
            report,
            state="approved",
            approver_id=str(actor),
            approved_at="2026-09-13T12:00:00+00:00",
            has_content_hash=True,
            claims=[
                {
                    "claim_id": str(claim),
                    "claim_type": "sole_signatory",
                    "actor_id": str(actor),
                    "recorded_at": "2026-09-13T11:59:00.123456+00:00",
                }
            ],
        )
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=body))
        result = adapter.get_signoff_state(session, report)
        assert result["approver_id"] == actor and result["has_content_hash"] is True
        assert result["claims"][0]["id"] == claim and result["claims"][0]["actor_id"] == actor

    def test_a_reason_in_the_answer_is_never_passed_on(self):
        report = uuid.uuid4()
        claim = {
            "claim_id": str(uuid.uuid4()),
            "claim_type": "sole_signatory",
            "actor_id": str(uuid.uuid4()),
            "recorded_at": "2026-09-13T11:59:00+00:00",
            "reason": "SHOULD-NOT-SURVIVE",
        }
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=_signoff_body(report, claims=[claim])))
        assert "SHOULD-NOT-SURVIVE" not in repr(adapter.get_signoff_state(session, report))

    @pytest.mark.parametrize(
        "mutate",
        [
            {"is_retracted": "no"},
            {"has_content_hash": 1},
            {"claims": {}},
            {"claims": [{"claim_id": "x"}]},
            {
                "claims": [
                    {
                        "claim_id": str(uuid.uuid4()),
                        "claim_type": "t",
                        "actor_id": "not-a-uuid",
                        "recorded_at": "2026-01-01T00:00:00+00:00",
                    }
                ]
            },
            {"approved_at": "yesterday"},
            {"approver_id": "not-a-uuid"},
            {"interpretation_id": "nope"},
            {"report_id": str(uuid.uuid4())},
        ],
    )
    def test_a_malformed_answer_is_an_error_not_a_partial_picture(self, mutate):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=_signoff_body(report, **mutate)))
        with pytest.raises(ClinicalServiceError):
            adapter.get_signoff_state(session, report)

    @pytest.mark.parametrize("missing", ["state", "claims", "is_retracted", "has_content_hash", "interpretation_id"])
    def test_a_missing_field_is_an_error(self, missing):
        report = uuid.uuid4()
        body = _signoff_body(report)
        body.pop(missing)
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ClinicalServiceError):
            adapter.get_signoff_state(session, report)

    def test_a_non_json_body_is_an_error(self):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, text="<html>"))
        with pytest.raises(ClinicalServiceError):
            adapter.get_signoff_state(session, uuid.uuid4())


def _approval(**over):
    """A complete, consistent approval block (approver, time and hash all present)."""
    block = {
        "approver_id": str(uuid.uuid4()),
        "approved_at": "2026-09-13T12:00:00+00:00",
        "has_content_hash": True,
    }
    block.update(over)
    return block


def _claim(**over):
    claim = {
        "claim_id": str(uuid.uuid4()),
        "claim_type": "sole_signatory",
        "actor_id": str(uuid.uuid4()),
        "recorded_at": "2026-09-13T11:00:00+00:00",
    }
    claim.update(over)
    return claim


class TestSignoffStateValidation:
    """What `get_signoff_state` accepts and what it refuses, against a stand-in (no database)."""

    @staticmethod
    def _read(**over):
        report = uuid.uuid4()
        adapter, session, seen = _logged_in(lambda r: httpx.Response(200, json=_signoff_body(report, **over)))
        return adapter.get_signoff_state(session, report), seen

    @staticmethod
    def _refused(**over):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=_signoff_body(report, **over)))
        with pytest.raises(ClinicalServiceError, match="malformed or inconsistent"):
            adapter.get_signoff_state(session, report)

    # ── legitimate answers are accepted, including incomplete ones ───────────────────────────
    @pytest.mark.parametrize("state", ["draft", "under_review", "returned"])
    def test_a_report_with_no_approval_is_legitimate_in_every_unapproved_state(self, state):
        result, _ = self._read(state=state)
        assert result["state"] == state and result["approver_id"] is None and result["approved_at"] is None
        assert result["has_content_hash"] is False

    def test_a_draft_with_a_claim_and_no_approval_is_legitimate(self):
        result, _ = self._read(claims=[_claim()])
        assert len(result["claims"]) == 1 and result["approver_id"] is None

    @pytest.mark.parametrize("state", ["approved", "released"])
    def test_an_approved_or_released_report_with_a_complete_approval_is_accepted(self, state):
        result, _ = self._read(state=state, **_approval())
        assert result["state"] == state and result["approver_id"] is not None
        assert result["approved_at"].utcoffset() is not None and result["has_content_hash"] is True

    def test_a_retracted_report_keeps_its_state(self):
        result, _ = self._read(state="approved", is_retracted=True, **_approval())
        assert result["is_retracted"] is True and result["state"] == "approved"

    @pytest.mark.parametrize(
        "claim_type", ["accept", "disagree", "variant_added", "variant_not_relevant", "sole_signatory"]
    )
    def test_every_claim_type_the_schema_allows_is_accepted(self, claim_type):
        result, _ = self._read(claims=[_claim(claim_type=claim_type)])
        assert result["claims"][0]["claim_type"] == claim_type

    def test_claims_in_order_including_equal_timestamps_are_accepted(self):
        stamps = ["2026-09-13T10:00:00+00:00", "2026-09-13T10:00:00+00:00", "2026-09-13T10:00:01.5+00:00"]
        result, _ = self._read(claims=[_claim(recorded_at=t) for t in stamps])
        assert [c["recorded_at"].isoformat() for c in result["claims"]][0] == "2026-09-13T10:00:00+00:00"

    def test_a_non_utc_offset_is_accepted_when_it_is_explicit(self):
        result, _ = self._read(claims=[_claim(recorded_at="2026-09-13T15:30:00+05:30")])
        assert result["claims"][0]["recorded_at"].utcoffset() is not None

    # ── unsupported or missing values ────────────────────────────────────────────────────────
    @pytest.mark.parametrize("state", ["archived", "", "APPROVED", "retracted", None, 3])
    def test_an_unsupported_state_is_refused(self, state):
        self._refused(state=state)

    @pytest.mark.parametrize("claim_type", ["", "concur", "SOLE_SIGNATORY", None, 7])
    def test_an_unsupported_claim_type_is_refused(self, claim_type):
        self._refused(claims=[_claim(claim_type=claim_type)])

    @pytest.mark.parametrize(
        "missing",
        [
            "report_id",
            "interpretation_id",
            "state",
            "is_retracted",
            "approver_id",
            "approved_at",
            "has_content_hash",
            "claims",
        ],
    )
    def test_every_top_level_field_is_required_even_when_null_is_legitimate(self, missing):
        report = uuid.uuid4()
        body = _signoff_body(report)
        body.pop(missing)
        adapter, session, _ = _logged_in(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ClinicalServiceError):
            adapter.get_signoff_state(session, report)

    @pytest.mark.parametrize("missing", ["claim_id", "claim_type", "actor_id", "recorded_at"])
    def test_every_claim_field_is_required(self, missing):
        claim = _claim()
        claim.pop(missing)
        self._refused(claims=[claim])

    # ── identifiers and timestamps ───────────────────────────────────────────────────────────
    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "1234", None, 5, ["x"]])
    def test_an_invalid_identifier_is_refused_everywhere_one_appears(self, bad):
        self._refused(interpretation_id=bad)
        self._refused(claims=[_claim(claim_id=bad)])
        self._refused(claims=[_claim(actor_id=bad)])
        if bad is not None:
            self._refused(**_approval(approver_id=bad))

    @pytest.mark.parametrize(
        "bad", ["yesterday", "", "2026-13-45T00:00:00+00:00", 20260913, ["2026-09-13T12:00:00+00:00"]]
    )
    def test_an_invalid_timestamp_is_refused(self, bad):
        self._refused(**_approval(approved_at=bad))
        self._refused(claims=[_claim(recorded_at=bad)])

    def test_a_timestamp_without_a_timezone_is_refused(self):
        self._refused(**_approval(approved_at="2026-09-13T12:00:00"))
        self._refused(claims=[_claim(recorded_at="2026-09-13T11:00:00")])

    # ── contradictions ───────────────────────────────────────────────────────────────────────
    @pytest.mark.parametrize("state", ["approved", "released"])
    def test_an_approved_or_released_report_without_approval_metadata_is_refused(self, state):
        self._refused(state=state)

    @pytest.mark.parametrize(
        "block",
        [
            {"approver_id": str(uuid.uuid4())},  # approver but no time and no hash
            {"approved_at": "2026-09-13T12:00:00+00:00"},  # time but no approver and no hash
            {"has_content_hash": True},  # hash but no approver and no time
            {"approver_id": str(uuid.uuid4()), "approved_at": "2026-09-13T12:00:00+00:00"},  # no hash
            {"approver_id": str(uuid.uuid4()), "has_content_hash": True},  # no time
            {"approved_at": "2026-09-13T12:00:00+00:00", "has_content_hash": True},  # no approver
        ],
    )
    def test_a_partial_approval_is_refused_in_any_state(self, block):
        for state in ("draft", "under_review", "approved"):
            self._refused(state=state, **block)

    def test_approved_with_an_approver_and_time_but_no_hash_is_refused(self):
        self._refused(state="approved", **_approval(has_content_hash=False))

    def test_a_repeated_claim_id_is_refused(self):
        claim = _claim()
        self._refused(claims=[claim, dict(claim, recorded_at="2026-09-13T11:00:05+00:00")])

    def test_claims_out_of_order_are_refused(self):
        self._refused(
            claims=[_claim(recorded_at="2026-09-13T11:00:05+00:00"), _claim(recorded_at="2026-09-13T11:00:00+00:00")]
        )

    @pytest.mark.parametrize("claims", [None, "x", {}, 3, [None], ["x"], [[]]])
    def test_claims_that_are_not_a_list_of_objects_are_refused(self, claims):
        self._refused(claims=claims)

    @pytest.mark.parametrize("flag", ["is_retracted", "has_content_hash"])
    @pytest.mark.parametrize("bad", ["true", 1, 0, None, "no"])
    def test_flags_must_be_booleans(self, flag, bad):
        self._refused(**{flag: bad})

    def test_the_answer_must_name_the_report_asked_about(self):
        self._refused(report_id=str(uuid.uuid4()))

    def test_a_refusal_never_returns_a_partial_result(self):
        report = uuid.uuid4()
        adapter, session, _ = _logged_in(
            lambda r: httpx.Response(200, json=_signoff_body(report, claims=[_claim(), _claim(claim_type="nope")]))
        )
        with pytest.raises(ClinicalServiceError):
            adapter.get_signoff_state(session, report)

    # ── still one read-only GET ──────────────────────────────────────────────────────────────
    def test_it_performs_exactly_one_get_and_no_write_whatever_the_answer(self):
        for answer in (_signoff_body(uuid.uuid4()), {"junk": 1}):
            report = uuid.uuid4()
            requests = []

            def handler(request, report=report, answer=answer):
                requests.append((request.method, request.url.path))
                return httpx.Response(
                    200, json=dict(answer, report_id=str(report)) if "report_id" in answer else answer
                )

            adapter, session, _ = _logged_in(handler)
            try:
                adapter.get_signoff_state(session, report)
            except ClinicalServiceError:
                pass
            assert requests == [("GET", f"/review/reports/{report}/signoff-state")]


class TestOnlyHttpsIsSpoken:
    @pytest.mark.parametrize("url", ["http://clinical.test", "ftp://x", "clinical.test", "", None])
    def test_a_non_https_address_is_refused(self, url):
        with pytest.raises(ClinicalServiceError, match="https://"):
            HttpClinicalDataAccess(url)

    def test_an_https_address_is_accepted_and_builds_no_connection_yet(self):
        adapter = HttpClinicalDataAccess("https://clinical.example/")
        adapter.close()


# ═════════════════════════════════════════════════════════════════════════════
# the tools use the adapter, never the database
# ═════════════════════════════════════════════════════════════════════════════


def _no_database(monkeypatch):
    import psycopg

    def forbidden(*args, **kwargs):
        raise AssertionError("a human-operated tool tried to open a database connection")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    assert not hasattr(s, "_connect_clinical_data_access"), "the human tools' database connector must not exist"
    monkeypatch.delenv("CLINICAL_DSN", raising=False)


class TestRefreshRevisionThroughTheService:
    def test_refresh_works_with_the_database_unreachable_and_matches_the_direct_path(
        self, signed, service, clinical, creds, dao, monkeypatch
    ):
        _r._retract(clinical, dao)
        direct = s.refresh_revision(signed, clinical_credentials=creds, clinical_data_access=dao)
        direct_status = _r._status(signed)
        direct_md = _r._md(signed)

        _no_database(monkeypatch)
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: _adapter(service))
        via_service = s.refresh_revision(signed, clinical_credentials=creds, clinical_url=URL)

        assert via_service["is_retracted"] is True and direct["is_retracted"] is True
        status = _r._status(signed)
        assert status["revision"] == direct_status["revision"] and status["report_id"] == direct_status["report_id"]
        assert _r._flat(_r.REASON) in _r._md(signed) and _r._flat(_r.REASON) in direct_md

    def test_the_signed_originals_are_untouched_and_no_credential_reaches_the_audit_log(
        self, signed, service, clinical, creds, monkeypatch
    ):
        before = _r._snapshot(signed, _r.SIGNED_FILES)
        _no_database(monkeypatch)
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: _adapter(service))
        s.refresh_revision(signed, clinical_credentials=creds, clinical_url=URL)
        assert _r._snapshot(signed, _r.SIGNED_FILES) == before
        log = open(os.path.join(signed, s.AUDIT_LOG_FILENAME), encoding="utf-8").read()
        assert creds.password not in log
        entry = _r._audit(signed)[-1]
        assert entry["action"] == "revision_refreshed" and not any("cookie" in k for k in entry)

    def test_no_service_address_and_no_injected_access_is_a_refusal_not_a_database_connection(
        self, signed, creds, monkeypatch
    ):
        _no_database(monkeypatch)
        monkeypatch.delenv(s.CLINICAL_URL_ENV, raising=False)
        before = _r._snapshot(signed, _r.SIGNED_FILES + _r.DERIVED_FILES)
        with pytest.raises(SignoffError, match="never connects to the database"):
            s.refresh_revision(signed, clinical_credentials=creds)
        assert _r._snapshot(signed, _r.SIGNED_FILES + _r.DERIVED_FILES) == before

    def test_the_address_may_come_from_the_environment(self, signed, service, creds, monkeypatch):
        _no_database(monkeypatch)
        seen = []
        monkeypatch.setenv(s.CLINICAL_URL_ENV, URL)
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: (seen.append(url), _adapter(service))[1])
        s.refresh_revision(signed, clinical_credentials=creds)
        assert seen == [URL]

    def test_a_service_failure_writes_nothing_and_is_audited(self, signed, service, clinical, creds, dao, monkeypatch):
        _no_database(monkeypatch)

        def explode(session, report_id):
            raise RuntimeError("database fell over")

        monkeypatch.setattr(dao, "get_report_revision", explode)
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: _adapter(service))
        before = _r._snapshot(signed, _r.SIGNED_FILES + _r.DERIVED_FILES)
        with pytest.raises(RevisionStatusError):
            s.refresh_revision(signed, clinical_credentials=creds, clinical_url=URL)
        assert _r._snapshot(signed, _r.SIGNED_FILES + _r.DERIVED_FILES) == before
        assert _r._audit(signed)[-1]["action"] == "revision_refresh_failed"

    def test_the_cli_takes_the_address_and_passes_it_on(self, signed, creds, monkeypatch):
        from review import cli

        seen = {}
        monkeypatch.setattr(cli, "_refresh_revision", lambda **kwargs: seen.update(kwargs) or {"ok": True})
        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", creds.password)
        assert (
            cli.main(
                ["refresh-revision", "--output-dir", signed, "--clinical-email", creds.email, "--clinical-url", URL]
            )
            == 0
        )
        assert seen["clinical_url"] == URL


class TestLimsExportStatusThroughTheService:
    def _run(self, directory, service, creds, monkeypatch, tmp_path, with_url=True):
        _no_database(monkeypatch)
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: _adapter(service))
        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", creds.password)
        out = str(tmp_path / "export.json")
        argv = [
            "export_lims",
            os.path.join(directory, s.RESULTS_FILENAME),
            "--json",
            out,
            "--clinical-email",
            creds.email,
        ]
        if with_url:
            argv += ["--clinical-url", URL]
        monkeypatch.setattr("sys.argv", argv)
        lims._main()
        return out

    def test_a_retracted_report_exports_with_its_status_read_over_the_service(
        self, signed, service, clinical, creds, dao, monkeypatch, tmp_path
    ):
        _r._retract(clinical, dao)
        out = self._run(signed, service, creds, monkeypatch, tmp_path)
        data = json.loads(open(out, encoding="utf-8").read())
        assert data["export_format_version"] == "1.1.0" and data["report_status"]["is_retracted"] is True

    def test_without_a_service_address_a_linked_export_is_refused(self, signed, service, creds, monkeypatch, tmp_path):
        monkeypatch.delenv(s.CLINICAL_URL_ENV, raising=False)
        with pytest.raises(LIMSExportBlockedError, match="never exported without its status"):
            self._run(signed, service, creds, monkeypatch, tmp_path, with_url=False)
        assert not (tmp_path / "export.json").exists()

    def test_an_unreadable_revision_blocks_the_export(
        self, signed, service, clinical, creds, dao, monkeypatch, tmp_path
    ):
        _r._retract(clinical, dao)

        def broken(session, report_id):
            raise BrokenRevisionRecordError("unreadable")

        monkeypatch.setattr(dao, "get_report_revision", broken)
        with pytest.raises(LIMSExportBlockedError):
            self._run(signed, service, creds, monkeypatch, tmp_path)
        assert not (tmp_path / "export.json").exists()
