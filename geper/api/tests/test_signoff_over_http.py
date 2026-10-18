"""
api/tests/test_signoff_over_http.py
───────────────────────────────────

`approve`, `override` and `withdraw` recording their clinical half through the clinical service's authenticated HTTPS
API, with NO database connection on the machine the person works at.

THE REAL SERVICE AND THE REAL TOOL. The real clinical FastAPI app and DataAccess over a real PostgreSQL
(api/tests/conftest.py::clinical_conn), reached by the real `review/signoff.py` through `HttpClinicalDataAccess`.
`psycopg.connect` is replaced with a function that fails the test, so a tool that quietly fell back to a database
connection cannot pass. (The service's own connection is the test fixture's, opened before that replacement.)

WHAT IS PINNED
  * the sign-off, disagreement claim and withdrawal reach the clinical record with the same effect as the direct path
    (state, claims, approver, content hash, audit rows) and the same files;
  * unauthenticated, unauthorised (wrong role), cross-organisation, malformed/unknown ids: refused, with the clinical
    record and the run directory exactly as they were (or, where the clinical layer had already advanced before the
    refusal, advanced exactly as far as the direct path advances it -- asserted, not assumed);
  * stale / repeated actions are refused by the domain's own state rules;
  * a write whose outcome is unknown is NEVER retried and writes no file;
  * the CLI forwards --clinical-url, and refuses without one rather than connecting to a database.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from api.tests import test_report_revision_refresh as _r
from api.tests import test_signoff_clinical_link as _base
from api.tests import test_clinical_http_adapter as _http
from clinical.data_access import (
    AuthenticationError,
    AuthorizationError,
    IncompleteClinicianIdentityError,
    NotFoundError,
    SessionExpired,
)
from review import signoff as s
from review.clinical_http import ClinicalServiceError, ClinicalWriteOutcomeUnknown, HttpClinicalDataAccess
from utils.exceptions import SignoffError

dao = _base.dao
service = _http.service
URL = _http.URL
_no_database = _http._no_database
clinical = _base.clinical
run_dir = _base.run_dir
linked_run_dir = _base.linked_run_dir
creds = _base.creds

_report_row = _base._report_row
_claims = _base._claims
_directory_state = _base._directory_state
_document = _base._document

REASON = "Sole signatory concurrence."


def _wire(monkeypatch, service):
    """Point the tools' HTTPS seam at the in-process service (a fresh client per call, as a real run has) and
    forbid any database connection."""
    _no_database(monkeypatch)
    monkeypatch.setattr(
        s,
        "_open_clinical_http",
        lambda url: HttpClinicalDataAccess(client=TestClient(service.app, base_url=URL, raise_server_exceptions=False)),
    )


def _audit(conn, actions):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, user_id, org_id, outcome FROM audit_log WHERE action = ANY(%s) ORDER BY 1, 2",
            (list(actions),),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _creds(email):
    return s.ClinicalCredentials(email=email, password="password")


APPROVAL_AUDIT = ("record_sole_signatory", "report_submitted_for_review", "report_approved")


# ═════════════════════════════════════════════════════════════════════════════
# approve
# ═════════════════════════════════════════════════════════════════════════════


class TestApproveOverHttp:
    def test_it_signs_the_clinical_record_and_the_files_with_no_database_connection(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        manifest = s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)

        state, approver_id, approved_at, content_hash = _report_row(clinical_conn, clinical["report_id"])
        assert state == "approved" and approved_at is not None and content_hash is not None
        assert approver_id == clinical["signatory"].user_id
        claims = _claims(clinical_conn, clinical["report_id"])
        assert len(claims) == 1 and claims[0][0] == "sole_signatory" and claims[0][4] == REASON
        assert claims[0][3] == clinical["signatory"].user_id

        document = _document(linked_run_dir)
        assert document["review_status"] == "reviewed"
        assert (
            document["reviewed_by"]
            == f"{_base.SIGNATORY_NAME}, Reg. No. {_base.SIGNATORY_REG}, {_base.SIGNATORY_HOSPITAL}"
        )
        assert manifest["identity_source"] == "user_record"
        assert manifest["clinical"]["approver_user_id"] == str(approver_id)

    def test_the_audit_trail_names_the_session_user_and_organisation_for_every_step(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        rows = _audit(clinical_conn, APPROVAL_AUDIT)
        by_action = {action: (user, org, outcome) for action, user, org, outcome in rows if outcome == "success"}
        assert set(by_action) == set(APPROVAL_AUDIT)
        for user, org, _outcome in by_action.values():
            assert user == clinical["signatory"].user_id and org == clinical["org_id"]

    def test_the_effect_is_the_same_as_the_direct_path(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        """The comparable facts agree with the literals test_signoff_clinical_link.py pins for the direct path: the
        approval is a `sole_signatory` claim with the stated reason by the session user, on a report now 'approved'."""
        _wire(monkeypatch, service)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"
        claims = [(c[0], c[1], c[2], c[3], c[4]) for c in _claims(clinical_conn, clinical["report_id"])]
        assert claims == [("sole_signatory", None, None, clinical["signatory"].user_id, REASON)]

    def test_without_credentials_nothing_changes(self, linked_run_dir, clinical, clinical_conn, service, monkeypatch):
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="authenticated person"):
            s.approve(linked_run_dir, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_without_a_service_address_it_is_refused_and_does_not_look_for_a_database(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        monkeypatch.delenv(s.CLINICAL_URL_ENV, raising=False)
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://never-used@invalid/none")
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="never connects to the database"):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON)
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_a_wrong_password_is_refused_and_nothing_changes(
        self, linked_run_dir, clinical, clinical_conn, service, monkeypatch
    ):
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        bad = s.ClinicalCredentials(email="signatory@org-a.test", password="not-the-password")
        with pytest.raises(AuthenticationError):
            s.approve(linked_run_dir, clinical_credentials=bad, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_a_signatory_without_the_approver_role_is_refused_at_approval_and_no_file_is_written(
        self, linked_run_dir, clinical, clinical_conn, service, monkeypatch
    ):
        """The Interpreter-only account may record the concurrence and submit (the Interpreter's acts) but is refused at
        approval by the domain's own role gate. That is the SAME partial progress the direct path makes; what matters
        is the refusal and that no signed file exists."""
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(AuthorizationError):
            s.approve(
                linked_run_dir,
                clinical_credentials=_creds("interpreter@org-a.test"),
                clinical_reason=REASON,
                clinical_url=URL,
            )
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "under_review"

    def test_an_account_with_no_reading_role_is_refused_before_any_write(
        self, linked_run_dir, clinical, clinical_conn, service, dao, monkeypatch
    ):
        _base._login_with_roles(
            dao, clinical_conn, clinical["org_id"], clinical["admin"], "orderer@org-a.test", ("Orderer",)
        )
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        # refused at the first step it fails: this account has no printed identity, and no reading role either
        with pytest.raises((AuthorizationError, SignoffError, IncompleteClinicianIdentityError)):
            s.approve(
                linked_run_dir,
                clinical_credentials=_creds("orderer@org-a.test"),
                clinical_reason=REASON,
                clinical_url=URL,
            )
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_a_link_naming_another_organisations_report_changes_nothing_anywhere(
        self, linked_run_dir, clinical, clinical_conn, service, creds, dao, monkeypatch
    ):
        other = _r._second_organisation(dao, clinical_conn)
        _r._write_link(
            linked_run_dir, report_id=str(other["report_id"]), interpretation_id=str(other["interpretation_id"])
        )
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        for report_id in (other["report_id"], clinical["report_id"]):
            assert _claims(clinical_conn, report_id) == []
            assert _report_row(clinical_conn, report_id)[0] == "draft"

    def test_a_report_that_is_not_that_interpretations_changes_nothing(
        self, linked_run_dir, clinical, clinical_conn, service, creds, dao, monkeypatch
    ):
        other = _r._second_organisation(dao, clinical_conn)
        # the REAL report id with another interpretation's id: the link and the record disagree
        _r._write_link(linked_run_dir, interpretation_id=str(other["interpretation_id"]))
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="interpretation"):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_a_retracted_or_unreadable_revision_refuses_before_any_clinical_write(
        self, linked_run_dir, clinical, clinical_conn, service, creds, dao, monkeypatch
    ):
        from clinical.data_access import BrokenRevisionRecordError

        def broken(session, report_id):
            raise BrokenRevisionRecordError("unreadable")

        monkeypatch.setattr(dao, "get_report_revision", broken)
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_a_second_approval_of_the_same_run_is_refused_and_the_first_stands(
        self, linked_run_dir, clinical, clinical_conn, service, creds, dao, monkeypatch
    ):
        """The signed state, approver, time, hash and every file are untouched by a repeat. The domain DOES append the
        repeat's concurrence claim before the submission is refused; that is `record_sole_signatory_claim`'s own
        behaviour on an approved report and identical on the direct path (asserted below, not assumed)."""
        _wire(monkeypatch, service)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        approved = _report_row(clinical_conn, clinical["report_id"])
        files = {k: v for k, v in _directory_state(linked_run_dir).items() if k != s.AUDIT_LOG_FILENAME}
        with pytest.raises(Exception):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason="again", clinical_url=URL)
        assert _report_row(clinical_conn, clinical["report_id"]) == approved
        assert {k: v for k, v in _directory_state(linked_run_dir).items() if k != s.AUDIT_LOG_FILENAME} == files
        over_http = len(_claims(clinical_conn, clinical["report_id"]))
        with pytest.raises(Exception):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason="direct", clinical_data_access=dao)
        assert len(_claims(clinical_conn, clinical["report_id"])) == over_http + (over_http - 1), (
            "the direct path appends the repeat's claim exactly as the HTTP path does"
        )
        assert _report_row(clinical_conn, clinical["report_id"]) == approved

    def test_the_session_is_ended_after_the_clinical_half(self, linked_run_dir, service, creds, monkeypatch):
        _wire(monkeypatch, service)
        opened = []
        real = s._open_clinical_http

        def spy(url):
            access = real(url)
            opened.append(access)
            return access

        monkeypatch.setattr(s, "_open_clinical_http", spy)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert len(opened) == 1 and opened[0]._session is None


# ═════════════════════════════════════════════════════════════════════════════
# override
# ═════════════════════════════════════════════════════════════════════════════


class TestOverrideOverHttp:
    ARGS = (
        "17:43106534:C>A",
        "Likely Pathogenic",
        "Segregation data in two affected relatives.",
        "signatory@org-a.test",
    )

    def test_it_records_the_disagreement_claim_and_changes_the_files(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        record = s.override(linked_run_dir, *self.ARGS, clinical_credentials=creds, clinical_url=URL)
        rows = _claims(clinical_conn, clinical["report_id"])
        assert len(rows) == 1
        claim_type, variant_key, classification, actor_id, reason = rows[0]
        assert (claim_type, variant_key, classification) == ("disagree", "17-43106534-C-A", "Likely Pathogenic")
        assert actor_id == clinical["signatory"].user_id and reason == self.ARGS[2]
        assert record["clinical"]["claim_type"] == "disagree"
        assert _document(linked_run_dir)["review_status"] == "overridden"
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"
        audited = _audit(clinical_conn, ("record_disagreement",))
        assert [(u, o) for _a, u, o, out in audited if out == "success"] == [
            (clinical["signatory"].user_id, clinical["org_id"])
        ]

    def test_an_approved_report_refuses_before_any_write(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="already 'approved'"):
            s.override(linked_run_dir, *self.ARGS, clinical_credentials=creds, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert len(_claims(clinical_conn, clinical["report_id"])) == 1  # only the sign-off's own claim

    def test_unauthenticated_and_unauthorised_callers_write_nothing(
        self, linked_run_dir, clinical, clinical_conn, service, dao, monkeypatch
    ):
        _base._login_with_roles(
            dao, clinical_conn, clinical["org_id"], clinical["admin"], "auditor@org-a.test", ("Auditor",)
        )
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        bad = s.ClinicalCredentials(email="signatory@org-a.test", password="nope")
        with pytest.raises(AuthenticationError):
            s.override(linked_run_dir, *self.ARGS, clinical_credentials=bad, clinical_url=URL)
        with pytest.raises(AuthorizationError):  # an Auditor may READ the report but may not record a claim
            s.override(linked_run_dir, *self.ARGS, clinical_credentials=_creds("auditor@org-a.test"), clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_cross_organisation_link_writes_nothing(
        self, linked_run_dir, clinical, clinical_conn, service, creds, dao, monkeypatch
    ):
        other = _r._second_organisation(dao, clinical_conn)
        _r._write_link(
            linked_run_dir, report_id=str(other["report_id"]), interpretation_id=str(other["interpretation_id"])
        )
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError):
            s.override(linked_run_dir, *self.ARGS, clinical_credentials=creds, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, other["report_id"]) == [] and _claims(clinical_conn, clinical["report_id"]) == []

    def test_a_malformed_classification_is_refused_by_the_domain_and_nothing_changes(
        self, linked_run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        _wire(monkeypatch, service)
        before = _directory_state(linked_run_dir)
        with pytest.raises(Exception):
            s.override(
                linked_run_dir,
                "17:43106534:C>A",
                "",
                "reason",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_url=URL,
            )
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# withdraw
# ═════════════════════════════════════════════════════════════════════════════


class TestWithdrawOverHttp:
    @pytest.fixture
    def approved(self, linked_run_dir, service, creds, monkeypatch):
        _wire(monkeypatch, service)
        s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        return linked_run_dir

    def test_an_approved_clinical_report_refuses_and_nothing_changes(self, approved, clinical, clinical_conn, creds):
        before = _directory_state(approved)
        with pytest.raises(SignoffError, match="already 'approved'"):
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_credentials=creds,
                clinical_url=URL,
            )
        assert _directory_state(approved) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"

    def test_a_report_that_is_not_approved_is_withdrawn_over_the_api(
        self, run_dir, clinical, clinical_conn, service, creds, monkeypatch
    ):
        s.approve(run_dir, "Dr. Typed In", "MCI-55555", "Some Lab")  # signed before it was linked
        _base.json.dump(
            {
                "org_id": str(clinical["org_id"]),
                "interpretation_id": str(clinical["interpretation_id"]),
                "report_id": str(clinical["report_id"]),
            },
            open(os.path.join(run_dir, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8"),
        )
        _wire(monkeypatch, service)
        result = s.withdraw(
            run_dir,
            reason="Signed off in error.",
            actor="signatory@org-a.test",
            clinical_credentials=creds,
            clinical_url=URL,
        )
        assert result["clinical_report_state"] == "draft"
        assert _document(run_dir)["review_status"] == "draft"
        assert not os.path.exists(os.path.join(run_dir, s.MANIFEST_FILENAME))
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_without_a_service_address_a_linked_withdraw_is_refused_untouched(self, approved, creds, monkeypatch):
        monkeypatch.delenv(s.CLINICAL_URL_ENV, raising=False)
        before = _directory_state(approved)
        with pytest.raises(SignoffError, match="never connects to the database"):
            s.withdraw(approved, reason="x", actor="signatory@org-a.test", clinical_credentials=creds)
        assert _directory_state(approved) == before


# ═════════════════════════════════════════════════════════════════════════════
# the adapter's writes, against the real service
# ═════════════════════════════════════════════════════════════════════════════


class TestAdapterWritesAgainstTheRealService:
    def _login(self, service, clinical, email="signatory@org-a.test"):
        adapter = HttpClinicalDataAccess(client=service)
        return adapter, adapter.login(email, clinical["org_id"], "password")

    def test_the_four_writes_run_the_domain_state_machine(self, service, clinical, clinical_conn):
        adapter, session = self._login(service, clinical)
        claim = adapter.record_sole_signatory_claim(
            session, clinical["interpretation_id"], clinical["report_id"], REASON
        )
        assert isinstance(claim, uuid.UUID)
        adapter.submit_for_review(session, clinical["report_id"])
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "under_review"
        adapter.approve_report(session, clinical["report_id"])
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"

    def test_out_of_order_and_repeated_calls_are_the_domains_value_errors_and_change_nothing(
        self, service, clinical, clinical_conn
    ):
        adapter, session = self._login(service, clinical)
        with pytest.raises(ValueError):  # not under review yet
            adapter.approve_report(session, clinical["report_id"])
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"
        adapter.record_sole_signatory_claim(session, clinical["interpretation_id"], clinical["report_id"], REASON)
        adapter.submit_for_review(session, clinical["report_id"])
        adapter.approve_report(session, clinical["report_id"])
        approved = _report_row(clinical_conn, clinical["report_id"])
        for repeat in (
            lambda: adapter.approve_report(session, clinical["report_id"]),
            lambda: adapter.submit_for_review(session, clinical["report_id"]),
        ):
            with pytest.raises(ValueError):
                repeat()
        assert _report_row(clinical_conn, clinical["report_id"]) == approved  # approver, time and hash are write-once

    def test_blank_reason_is_refused_by_the_domain(self, service, clinical, clinical_conn):
        adapter, session = self._login(service, clinical)
        with pytest.raises(ValueError):
            adapter.record_sole_signatory_claim(session, clinical["interpretation_id"], clinical["report_id"], "   ")
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_unknown_report_ids_are_not_found_for_every_write(self, service, clinical):
        adapter, session = self._login(service, clinical)
        missing = uuid.uuid4()
        with pytest.raises(NotFoundError):
            adapter.submit_for_review(session, missing)
        with pytest.raises(NotFoundError):
            adapter.approve_report(session, missing)
        with pytest.raises((NotFoundError, ValueError)):
            adapter.record_sole_signatory_claim(session, clinical["interpretation_id"], missing, REASON)
        with pytest.raises((NotFoundError, ValueError)):
            adapter.record_disagreement_claim(
                session, clinical["interpretation_id"], missing, "17-1-A-C", "Pathogenic", REASON
            )

    def test_a_malformed_report_id_is_a_404_and_not_a_server_error(self, service, clinical):
        _adapter, _session = self._login(service, clinical)
        for path in ("approval", "submission"):
            response = service.post(f"/review/reports/not-a-uuid/{path}")
            assert response.status_code == 404

    def test_another_organisations_report_is_not_found_and_untouched(self, service, clinical, clinical_conn, dao):
        other = _r._second_organisation(dao, clinical_conn)
        adapter, session = self._login(service, clinical)
        with pytest.raises(NotFoundError):
            adapter.submit_for_review(session, other["report_id"])
        with pytest.raises((NotFoundError, ValueError)):
            adapter.record_sole_signatory_claim(session, other["interpretation_id"], other["report_id"], REASON)
        assert _claims(clinical_conn, other["report_id"]) == []
        assert _report_row(clinical_conn, other["report_id"])[0] == "draft"

    def test_an_interpretation_that_is_not_the_reports_is_refused_and_no_claim_is_written(
        self, service, clinical, clinical_conn, dao
    ):
        other = _r._second_organisation(dao, clinical_conn)
        adapter, session = self._login(service, clinical)
        with pytest.raises((NotFoundError, ValueError)):
            adapter.record_sole_signatory_claim(session, other["interpretation_id"], clinical["report_id"], REASON)
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_unauthenticated_writes_are_401_and_a_session_that_ended_is_session_expired(self, service, clinical):
        assert service.post(f"/review/reports/{clinical['report_id']}/approval").status_code == 401
        adapter, session = self._login(service, clinical)
        service.cookies.clear()
        with pytest.raises(SessionExpired):
            adapter.submit_for_review(session, clinical["report_id"])

    def test_a_body_naming_an_actor_or_organisation_is_refused_by_the_service(self, service, clinical):
        self._login(service, clinical)
        body = {"interpretation_id": str(clinical["interpretation_id"]), "reason": REASON}
        for extra in ({"actor_id": str(uuid.uuid4())}, {"org_id": str(uuid.uuid4())}, {"state": "approved"}):
            response = service.post(f"/review/reports/{clinical['report_id']}/concurrence", json={**body, **extra})
            assert response.status_code == 422

    def test_a_session_the_adapter_did_not_open_cannot_write(self, service, clinical):
        adapter, _session = self._login(service, clinical)
        with pytest.raises(ClinicalServiceError, match="not opened by this adapter"):
            adapter.approve_report(clinical["signatory"], clinical["report_id"])

    def test_the_adapter_offers_no_release_or_amendment(self):
        for name in ("release_report", "create_amendment", "retract_report", "withdraw_report"):
            assert not hasattr(HttpClinicalDataAccess, name)


# ═════════════════════════════════════════════════════════════════════════════
# write error mapping and the unknown-outcome rule, against a stand-in server
# ═════════════════════════════════════════════════════════════════════════════


def _stub_writer(handler):
    """Logged in against a stand-in; returns (adapter, session, requests seen after login)."""
    user, org = uuid.uuid4(), uuid.uuid4()
    seen = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(204)
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"user_id": str(user), "org_id": str(org)})
        seen.append(request)
        return handler(request)

    adapter = HttpClinicalDataAccess(client=httpx.Client(base_url=URL, transport=httpx.MockTransport(route)))
    return adapter, adapter.login("a@b.test", org, "pw"), seen


WRITES = {
    "concurrence": (lambda a, s_: a.record_sole_signatory_claim(s_, uuid.uuid4(), uuid.uuid4(), "r"), 201),
    "disagreement": (
        lambda a, s_: a.record_disagreement_claim(s_, uuid.uuid4(), uuid.uuid4(), "1-1-A-C", "Pathogenic", "r"),
        201,
    ),
    "submission": (lambda a, s_: a.submit_for_review(s_, uuid.uuid4()), 204),
    "approval": (lambda a, s_: a.approve_report(s_, uuid.uuid4()), 204),
}


class TestWriteErrorMapping:
    @pytest.mark.parametrize("write", sorted(WRITES))
    @pytest.mark.parametrize(
        "status, exception",
        [
            (400, ValueError),
            (401, SessionExpired),
            (403, AuthorizationError),
            (404, NotFoundError),
            (422, ClinicalServiceError),
            (500, ClinicalWriteOutcomeUnknown),
            (502, ClinicalWriteOutcomeUnknown),
            (503, ClinicalWriteOutcomeUnknown),
            (200, ClinicalWriteOutcomeUnknown),  # a success status that is not the contract's is not a success
        ],
    )
    def test_statuses(self, write, status, exception):
        call, _ok = WRITES[write]
        adapter, session, seen = _stub_writer(lambda r: httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(exception) as caught:
            call(adapter, session)
        if status == 422:
            assert not isinstance(caught.value, ClinicalWriteOutcomeUnknown)  # a rejected request was not processed
        assert len(seen) == 1, "a write is never retried"

    @pytest.mark.parametrize("write", sorted(WRITES))
    def test_a_dropped_connection_is_an_unknown_outcome_that_is_not_retried_and_says_what_to_do(self, write):
        call, _ok = WRITES[write]

        def drop(request):
            raise httpx.ReadTimeout("timed out", request=request)

        adapter, session, _seen = _stub_writer(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=r)))
        attempts = []
        original = adapter._client.request

        def counting(*args, **kwargs):
            attempts.append(args)
            return original(*args, **kwargs)

        adapter._client.request = counting
        with pytest.raises(ClinicalWriteOutcomeUnknown, match="UNKNOWN.*not retried.*Check the report"):
            call(adapter, session)
        assert len(attempts) == 1

    def test_a_201_without_a_claim_id_is_not_treated_as_a_refusal(self):
        adapter, session, _ = _stub_writer(lambda r: httpx.Response(201, json={"nope": 1}))
        with pytest.raises(ClinicalWriteOutcomeUnknown, match="claim exists"):
            adapter.record_sole_signatory_claim(session, uuid.uuid4(), uuid.uuid4(), "r")

    def test_the_body_sent_carries_only_the_claimed_fields(self):
        adapter, session, seen = _stub_writer(lambda r: httpx.Response(201, json={"claim_id": str(uuid.uuid4())}))
        interpretation, report = uuid.uuid4(), uuid.uuid4()
        adapter.record_sole_signatory_claim(session, interpretation, report, "why")
        request = seen[0]
        assert request.url.path == f"/review/reports/{report}/concurrence"
        import json as _json

        assert _json.loads(request.content) == {"interpretation_id": str(interpretation), "reason": "why"}

    def test_the_tool_writes_no_file_when_the_approval_outcome_is_unknown(self, linked_run_dir, creds, monkeypatch):
        """approve() over a service that answers 500 to the approval: the claim and submission went through, the
        approval's outcome is unknown, and no signed file is written."""
        user, org = (
            uuid.uuid4(),
            uuid.UUID(_base.json.load(open(os.path.join(linked_run_dir, s.CLINICAL_LINK_FILENAME)))["org_id"]),
        )
        link = _base.json.load(open(os.path.join(linked_run_dir, s.CLINICAL_LINK_FILENAME)))

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/auth/login":
                return httpx.Response(204)
            if path == "/auth/me":
                return httpx.Response(200, json={"user_id": str(user), "org_id": str(org)})
            if path == "/auth/me/signing-identity":
                return httpx.Response(
                    200,
                    json={
                        "user_id": str(user),
                        "full_name": "Dr. A. Sharma, MD",
                        "registration_number": "MCI-12345",
                        "hospital": "ABC Diagnostics",
                    },
                )
            if path == f"/review/reports/{link['report_id']}":
                return httpx.Response(
                    200,
                    json={
                        "report_id": link["report_id"],
                        "interpretation_id": link["interpretation_id"],
                        "state": "draft",
                        "is_retracted": False,
                        "created_at": "2026-09-13T12:00:00+00:00",
                    },
                )
            if path.endswith("/revision"):
                return httpx.Response(200, json={"report_id": link["report_id"], "revision": {}})
            if path.endswith("/concurrence"):
                return httpx.Response(201, json={"claim_id": str(uuid.uuid4())})
            if path.endswith("/submission"):
                return httpx.Response(204)
            return httpx.Response(500, text="boom")

        monkeypatch.setattr(
            s,
            "_open_clinical_http",
            lambda url: HttpClinicalDataAccess(client=httpx.Client(base_url=URL, transport=httpx.MockTransport(route))),
        )
        before = _directory_state(linked_run_dir)
        with pytest.raises(ClinicalWriteOutcomeUnknown):
            s.approve(linked_run_dir, clinical_credentials=creds, clinical_reason=REASON, clinical_url=URL)
        assert _directory_state(linked_run_dir) == before
        assert not os.path.exists(os.path.join(linked_run_dir, s.MANIFEST_FILENAME))


# ═════════════════════════════════════════════════════════════════════════════
# the CLI
# ═════════════════════════════════════════════════════════════════════════════


class TestCli:
    def test_cli_approve_signs_a_linked_run_over_https_with_no_database(
        self, linked_run_dir, clinical, clinical_conn, service, monkeypatch
    ):
        from review.cli import main as cli_main

        _wire(monkeypatch, service)
        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        exit_code = cli_main(
            [
                "approve",
                "--output-dir",
                linked_run_dir,
                "--clinical-email",
                "signatory@org-a.test",
                "--clinical-url",
                URL,
                "--reason",
                "Sole signatory concurrence, via the CLI.",
            ]
        )
        assert exit_code == 0
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"
        assert _claims(clinical_conn, clinical["report_id"])[0][4] == "Sole signatory concurrence, via the CLI."
        assert _document(linked_run_dir)["review_status"] == "reviewed"

    def test_cli_without_a_service_address_refuses_and_changes_nothing(
        self, linked_run_dir, clinical, clinical_conn, service, monkeypatch
    ):
        from review.cli import main as cli_main

        _wire(monkeypatch, service)
        monkeypatch.delenv(s.CLINICAL_URL_ENV, raising=False)
        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        before = _directory_state(linked_run_dir)
        code = cli_main(
            ["approve", "--output-dir", linked_run_dir, "--clinical-email", "signatory@org-a.test", "--reason", "x"]
        )
        assert code == 1
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    @pytest.mark.parametrize("command", ["approve", "override", "withdraw", "refresh-revision"])
    def test_every_clinical_command_takes_the_service_address(self, command):
        from review.cli import build_arg_parser

        parser = build_arg_parser()
        required = {
            "approve": ["--clinician-name", "n", "--reg-number", "r", "--hospital", "h"],
            "override": [
                "--variant",
                "1:1:A>C",
                "--new-classification",
                "Pathogenic",
                "--reason",
                "r",
                "--clinician-id",
                "c",
            ],
            "withdraw": ["--reason", "r", "--actor", "a"],
            "refresh-revision": [],
        }[command]
        args = parser.parse_args([command, "--output-dir", "x", *required, "--clinical-url", URL])
        assert args.clinical_url == URL

    def test_the_cli_forwards_the_address_for_withdraw_and_override(self, tmp_path, monkeypatch):
        import review.cli as cli

        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        captured = {}
        for name in ("_withdraw", "_override"):
            monkeypatch.setattr(cli, name, lambda **kwargs: captured.update(kwargs) or {"ok": True})
        assert (
            cli.main(
                [
                    "withdraw",
                    "--output-dir",
                    str(tmp_path),
                    "--reason",
                    "r",
                    "--actor",
                    "a",
                    "--clinical-email",
                    "e@x.test",
                    "--clinical-url",
                    URL,
                ]
            )
            == 0
        )
        assert captured["clinical_url"] == URL
        captured.clear()
        assert (
            cli.main(
                [
                    "override",
                    "--output-dir",
                    str(tmp_path),
                    "--variant",
                    "1:1:A>C",
                    "--new-classification",
                    "Pathogenic",
                    "--reason",
                    "r",
                    "--clinician-id",
                    "c",
                    "--clinical-email",
                    "e@x.test",
                    "--clinical-url",
                    URL,
                ]
            )
            == 0
        )
        assert captured["clinical_url"] == URL
