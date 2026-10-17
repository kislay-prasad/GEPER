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

    def test_the_sign_off_writes_are_not_offered(self):
        adapter, session, _ = _logged_in(lambda r: httpx.Response(500))
        for name in ("submit_for_review", "record_sole_signatory_claim", "record_disagreement_claim", "approve_report"):
            with pytest.raises(ClinicalServiceError, match="not available through the HTTP adapter"):
                getattr(adapter, name)(session, uuid.uuid4())


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
    def forbidden():
        raise AssertionError("a human-operated tool tried to open a database connection")

    monkeypatch.setattr(s, "_connect_clinical_data_access", forbidden)
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
