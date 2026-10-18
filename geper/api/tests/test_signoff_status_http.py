"""
api/tests/test_signoff_status_http.py
─────────────────────────────────────

`review/signoff_status.py::run_status` and the `status` command, against a STAND-IN clinical service
(httpx.MockTransport) that records every request it receives.

THE GUARANTEE PINNED HERE: status reads. Every request that reaches a /review route is a GET. The only non-GET requests that
can occur at all are the credential exchange the existing adapter performs (`POST /auth/login`, and `POST /auth/logout` when
the adapter opened the session): they create and end a server-side session and change no report. No PUT, PATCH or DELETE, no
claim, no submission, no approval, ever.

ALSO PINNED: authentication failure, an unreachable service, malformed or self-contradictory answers, a server state that
changes between the two reads, an organisation mismatch, and that none of those yields a classification; and that the
directory is byte-for-byte unchanged afterwards.

Synthetic data only; no database, no real service.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest

from review import cli, signoff as signoff_module, signoff_status as st
from review.clinical_http import HttpClinicalDataAccess
from review.signoff import ClinicalCredentials
from tests.test_signoff_status import INTERP, ME, ORG, OTHER, REPORT, SIGNED, build_run

URL = "https://clinical.test"
STATE_PATH = f"/review/reports/{REPORT}/signoff-state"
PASSWORD = "pw-never-printed-3141"
CREDS = ClinicalCredentials(email="signatory@example.test", password=PASSWORD)


def body(state="draft", *, retracted=False, approver=None, claims=(), **over):
    approved = approver is not None
    out = {
        "report_id": str(REPORT),
        "interpretation_id": str(INTERP),
        "state": state,
        "is_retracted": retracted,
        "approver_id": str(approver) if approved else None,
        "approved_at": "2026-09-03T12:00:00+00:00" if approved else None,
        "has_content_hash": approved,
        "claims": [
            {
                "claim_id": str(uuid.uuid4()),
                "claim_type": kind,
                "actor_id": str(actor),
                "recorded_at": f"2026-09-03T11:0{i}:00+00:00",
            }
            for i, (kind, actor) in enumerate(claims)
        ],
    }
    out.update(over)
    return out


class StandIn:
    """A recording stand-in for the clinical service. `answers` is a list of (status, json-or-httpx.Response) consumed in order;
    the last repeats."""

    def __init__(self, answers, *, login_status=204, me_org=ORG, me_user=ME, raise_on_state=None):
        self.requests = []
        self.answers = list(answers)
        self.login_status, self.me_org, self.me_user, self.raise_on_state = (
            login_status,
            me_org,
            me_user,
            raise_on_state,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(self.login_status)
        if path == "/auth/me":
            return httpx.Response(200, json={"user_id": str(self.me_user), "org_id": str(self.me_org)})
        if path == "/auth/logout":
            return httpx.Response(204)
        if path == STATE_PATH and request.method == "GET":
            if self.raise_on_state is not None:
                raise self.raise_on_state
            status, payload = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
            if isinstance(payload, httpx.Response):
                return payload
            return httpx.Response(status, json=payload)
        return httpx.Response(405, json={"detail": "unexpected request in a read-only test"})

    def adapter(self) -> HttpClinicalDataAccess:
        return HttpClinicalDataAccess(client=httpx.Client(base_url=URL, transport=httpx.MockTransport(self.handler)))

    def non_get(self):
        return [r for r in self.requests if r[0] != "GET"]

    def review_requests(self):
        return [r for r in self.requests if r[1].startswith("/review")]


def status_of(run, service, creds=CREDS):
    return st.run_status(str(run), clinical_credentials=creds, data_access=service.adapter())


def linked_run(tmp_path, **kwargs):
    kwargs.setdefault("link", True)
    return build_run(tmp_path, **kwargs)


# ── classification through the real adapter ───────────────────────────────────────────────────────────────────────

OUTCOMES = {
    "draft_without_a_claim": (dict(), [(200, body())], ("CONSISTENT", "NOT_STARTED", 0)),
    "draft_with_an_existing_claim": (
        dict(),
        [(200, body(claims=[("sole_signatory", ME)]))],
        ("INCOMPLETE", "DRAFT_WITH_CLAIMS", 10),
    ),
    "under_review": (
        dict(),
        [(200, body("under_review", claims=[("sole_signatory", ME)]))],
        ("INCOMPLETE", "UNDER_REVIEW", 10),
    ),
    "approved_by_the_current_operator": (
        SIGNED,
        [(200, body("approved", approver=ME))],
        ("CONSISTENT", "SIGNED_OFF_BY_YOU", 0),
    ),
    "approved_by_another_operator": (
        dict(SIGNED, manifest_approver=OTHER),
        [(200, body("approved", approver=OTHER))],
        ("CONSISTENT", "SIGNED_OFF_BY_ANOTHER_OPERATOR", 0),
    ),
    "released": (SIGNED, [(200, body("released", approver=ME))], ("CONSISTENT", "RELEASED_CONSISTENT_BY_YOU", 0)),
    "retracted": (SIGNED, [(200, body("approved", approver=ME, retracted=True))], ("INVESTIGATE", "RETRACTED", 20)),
    "report_not_in_the_callers_organisation": (
        dict(),
        [(404, {"detail": "not found"})],
        ("INVESTIGATE", "REPORT_NOT_FOUND_IN_ORGANISATION", 20),
    ),
    "approved_but_reviewed_json_has_no_manifest": (
        dict(status="reviewed"),
        [(200, body("approved", approver=ME))],
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", 20),
    ),
    "approved_but_manifest_invalid": (
        dict(SIGNED, manifest="pdf_changed"),
        [(200, body("approved", approver=ME))],
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", 20),
    ),
    "approved_but_local_never_signed": (
        dict(),
        [(200, body("approved", approver=ME))],
        ("INVESTIGATE", "APPROVED_NO_LOCAL_SIGNOFF_BY_YOU", 20),
    ),
    "local_reviewed_but_server_only_draft": (
        dict(status="reviewed"),
        [(200, body())],
        ("INVESTIGATE", "LOCAL_REVIEWED_SERVER_NOT_APPROVED", 20),
    ),
    "server_names_another_interpretation": (
        dict(),
        [(200, body(interpretation_id=str(uuid.uuid4())))],
        ("INVESTIGATE", "WRONG_TARGET", 20),
    ),
}


@pytest.mark.parametrize("name", sorted(OUTCOMES))
def test_outcome(tmp_path, name):
    run_kwargs, answers, (group, code, exit_code) = OUTCOMES[name]
    service = StandIn(answers)
    result = status_of(linked_run(tmp_path, **run_kwargs), service)
    assert (result.group, result.code, result.exit_code) == (group, code, exit_code)


def test_the_hold_flag_accompanies_a_reviewed_json_without_a_valid_manifest(tmp_path):
    result = status_of(linked_run(tmp_path, status="reviewed"), StandIn([(200, body("approved", approver=ME))]))
    assert result.hold is True and any("HOLD" in step for step in result.next_steps)


# ── GET only ──────────────────────────────────────────────────────────────────────────────────────────────────────

ALL_SCENARIOS = [OUTCOMES[n] for n in sorted(OUTCOMES)] + [
    (dict(), [(401, {"detail": "x"})], None),
    (dict(), [(403, {"detail": "x"})], None),
    (dict(), [(500, {"detail": "x"})], None),
    (dict(), [(200, [1, 2, 3])], None),
]


@pytest.mark.parametrize("index", range(len(ALL_SCENARIOS)))
def test_no_request_to_a_review_route_is_anything_but_a_get(tmp_path, index):
    run_kwargs, answers, _ = ALL_SCENARIOS[index]
    service = StandIn(answers)
    status_of(linked_run(tmp_path, **run_kwargs), service)
    assert service.review_requests(), "the scenario should have reached the service"
    assert all(method == "GET" for method, path in service.review_requests())
    assert {m for m, _ in service.requests} <= {"GET", "POST"}
    assert set(service.non_get()) <= {
        ("POST", "/auth/login")
    }  # the credential exchange only (no logout: the adapter was injected)
    assert not [r for r in service.requests if r[0] in ("PUT", "PATCH", "DELETE")]


def test_a_readable_state_costs_exactly_two_gets_of_the_state_route(tmp_path):
    service = StandIn([(200, body())])
    status_of(linked_run(tmp_path), service)
    assert service.requests.count(("GET", STATE_PATH)) == 2


def test_through_the_command_the_session_is_ended_and_only_login_and_logout_are_posts(tmp_path, monkeypatch, capsys):
    service = StandIn([(200, body("approved", approver=ME))])
    monkeypatch.setattr(signoff_module, "_open_clinical_http", lambda url: service.adapter())
    monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", PASSWORD)
    code = cli.main(
        [
            "status",
            str(linked_run(tmp_path, **SIGNED)),
            "--clinical-email",
            "signatory@example.test",
            "--clinical-url",
            URL,
        ]
    )
    assert code == 0
    assert set(service.non_get()) == {("POST", "/auth/login"), ("POST", "/auth/logout")}
    assert all(m == "GET" for m, p in service.review_requests())
    out = capsys.readouterr().out
    assert PASSWORD not in out and "SIGNED_OFF_BY_YOU" in out


# ── cannot establish the state: no classification is offered ─────────────────────────────────────────────────────

UNREADABLE = {
    "login_refused": (lambda: StandIn([(200, body())], login_status=401), "AUTHENTICATION_FAILED"),
    "login_rate_limited": (lambda: StandIn([(200, body())], login_status=429), "AUTHENTICATION_FAILED"),
    "session_expired_on_read": (lambda: StandIn([(401, {"detail": "x"})]), "SESSION_EXPIRED"),
    "permission_refused": (lambda: StandIn([(403, {"detail": "x"})]), "PERMISSION_DENIED"),
    "service_error_500": (lambda: StandIn([(500, {"detail": "SECRET-SERVER-DETAIL"})]), "SERVICE_UNREADABLE"),
    "service_error_503": (lambda: StandIn([(503, {"detail": "x"})]), "SERVICE_UNREADABLE"),
    "service_unreachable": (
        lambda: StandIn([(200, body())], raise_on_state=httpx.ConnectError("SECRET-HOST-DETAIL")),
        "SERVICE_UNREADABLE",
    ),
    "service_times_out": (
        lambda: StandIn([(200, body())], raise_on_state=httpx.ReadTimeout("slow")),
        "SERVICE_UNREADABLE",
    ),
    "organisation_mismatch_at_login": (lambda: StandIn([(200, body())], me_org=uuid.uuid4()), "SERVICE_UNREADABLE"),
    "answer_is_not_json": (lambda: StandIn([(200, httpx.Response(200, content=b"<html>"))]), "SERVICE_UNREADABLE"),
    "answer_is_a_list": (lambda: StandIn([(200, [])]), "SERVICE_UNREADABLE"),
    "field_missing": (
        lambda: StandIn([(200, {k: v for k, v in body().items() if k != "claims"})]),
        "SERVICE_UNREADABLE",
    ),
    "unknown_state": (lambda: StandIn([(200, body("archived"))]), "SERVICE_UNREADABLE"),
    "claims_not_a_list": (lambda: StandIn([(200, {**body(), "claims": "x"})]), "SERVICE_UNREADABLE"),
    "approval_fields_disagree": (
        lambda: StandIn([(200, body(approver=None, approved_at="2026-09-03T12:00:00+00:00"))]),
        "SERVICE_UNREADABLE",
    ),
    "approved_without_an_approver": (lambda: StandIn([(200, body("approved"))]), "SERVICE_UNREADABLE"),
    "answer_names_another_report": (lambda: StandIn([(200, body(report_id=str(uuid.uuid4())))]), "SERVICE_UNREADABLE"),
    "unknown_claim_type": (lambda: StandIn([(200, body(claims=[("mystery", ME)]))]), "SERVICE_UNREADABLE"),
}


@pytest.mark.parametrize("name", sorted(UNREADABLE))
def test_unreadable_states_offer_no_classification(tmp_path, name, capsys):
    factory, code = UNREADABLE[name]
    service = factory()
    run = linked_run(tmp_path, **SIGNED)
    result = status_of(run, service)
    assert (result.group, result.code, result.exit_code) == ("UNREADABLE", code, 30)
    assert result.server is None  # no server facts were trusted
    text = st.format_text(result)
    for leak in ("SECRET-SERVER-DETAIL", "SECRET-HOST-DETAIL", PASSWORD):
        assert leak not in text and leak not in json.dumps(result.to_dict())
    assert "NOT VERIFIED" in text


def test_a_state_that_changes_between_the_two_reads_is_not_classified(tmp_path):
    service = StandIn([(200, body()), (200, body("under_review", claims=[("sole_signatory", ME)]))])
    result = status_of(linked_run(tmp_path), service)
    assert (result.code, result.exit_code) == ("SERVER_STATE_CHANGED_DURING_READ", 30)


def test_a_claim_appearing_between_the_two_reads_is_not_classified(tmp_path):
    service = StandIn([(200, body()), (200, body(claims=[("sole_signatory", ME)]))])
    assert status_of(linked_run(tmp_path), service).code == "SERVER_STATE_CHANGED_DURING_READ"


def test_a_vanishing_report_between_reads_is_not_classified(tmp_path):
    service = StandIn([(200, body()), (404, {"detail": "gone"})])
    assert status_of(linked_run(tmp_path), service).code == "SERVER_STATE_CHANGED_DURING_READ"


def test_an_unexpected_failure_reports_only_its_type(tmp_path):
    service = StandIn([(200, body())], raise_on_state=RuntimeError("SECRET-TOKEN-IN-MESSAGE"))
    result = status_of(linked_run(tmp_path), service)
    assert result.code == "UNEXPECTED_ERROR" and "RuntimeError" in result.summary
    assert "SECRET-TOKEN-IN-MESSAGE" not in st.format_text(result)


# ── identity and organisation ─────────────────────────────────────────────────────────────────────────────────────


class _FakeAccess:
    """An injected access object that, unlike the HTTP adapter, does not itself check the organisation."""

    def __init__(self, session_org, state):
        self._session = SimpleNamespace(user_id=ME, org_id=session_org)
        self._state = state

    def login(self, email, org_id, password, totp_code=None):
        return self._session

    def get_signoff_state(self, session, report_id):
        return self._state


def _parsed(state="draft"):
    from review.clinical_http import _parse_signoff_state

    return _parse_signoff_state(body(state), REPORT)


def test_a_session_in_another_organisation_than_the_link_is_a_contradiction(tmp_path):
    result = st.run_status(
        str(linked_run(tmp_path)), clinical_credentials=CREDS, data_access=_FakeAccess(uuid.uuid4(), _parsed())
    )
    assert (result.group, result.code, result.exit_code) == ("INVESTIGATE", "ORGANISATION_MISMATCH", 20)


def test_a_matching_session_is_classified(tmp_path):
    result = st.run_status(
        str(linked_run(tmp_path)), clinical_credentials=CREDS, data_access=_FakeAccess(ORG, _parsed())
    )
    assert result.code == "NOT_STARTED"


def test_an_access_object_that_cannot_read_the_state_is_unreadable(tmp_path):
    class NoReader:
        def login(self, *a, **k):
            return SimpleNamespace(user_id=ME, org_id=ORG)

    result = st.run_status(str(linked_run(tmp_path)), clinical_credentials=CREDS, data_access=NoReader())
    assert result.code == "SERVICE_UNREADABLE"


def test_approval_by_the_operator_is_decided_by_the_authenticated_identity(tmp_path):
    answers = [(200, body("approved", approver=ME))]
    run = linked_run(tmp_path, **SIGNED)
    as_me = status_of(run, StandIn(answers, me_user=ME))
    as_someone_else = status_of(run, StandIn(answers, me_user=OTHER))
    assert (as_me.code, as_someone_else.code) == ("SIGNED_OFF_BY_YOU", "SIGNED_OFF_BY_ANOTHER_OPERATOR")


# ── links ─────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content, code",
    [("{broken", "LINK_UNREADABLE"), ("[1]", "LINK_UNREADABLE"), (json.dumps({"org_id": str(ORG)}), "LINK_INCOMPLETE")],
)
def test_a_malformed_link_is_unreadable_and_never_reaches_the_service(tmp_path, content, code):
    run = linked_run(tmp_path)
    (run / "clinical_link.json").write_text(content, encoding="utf-8")
    service = StandIn([(200, body())])
    assert status_of(run, service).code == code
    assert service.requests == []


def test_a_link_with_a_malformed_identifier_is_unreadable(tmp_path):
    run = linked_run(tmp_path)
    (run / "clinical_link.json").write_text(
        json.dumps({"org_id": "x", "interpretation_id": "y", "report_id": "z"}), encoding="utf-8"
    )
    service = StandIn([(200, body())])
    assert status_of(run, service).code == "LINK_INCOMPLETE" and service.requests == []


def test_missing_credentials_never_reach_the_service(tmp_path):
    service = StandIn([(200, body())])
    result = st.run_status(str(linked_run(tmp_path)), clinical_credentials=None, data_access=service.adapter())
    assert result.code == "CREDENTIALS_NOT_SUPPLIED" and service.requests == []


# ── output hygiene ────────────────────────────────────────────────────────────────────────────────────────────────


def test_fields_the_service_should_not_send_are_never_echoed(tmp_path):
    hostile = body("approved", approver=ME, reason="PATIENT-NAME-IN-A-REASON", patient_name="PATIENT-NAME")
    hostile["claims"].append(
        {
            "claim_id": str(uuid.uuid4()),
            "claim_type": "accept",
            "actor_id": str(OTHER),
            "recorded_at": "2026-09-03T11:30:00+00:00",
            "reason": "CLAIM-REASON-TEXT",
        }
    )
    result = status_of(linked_run(tmp_path, **SIGNED), StandIn([(200, hostile)]))
    text, blob = st.format_text(result), json.dumps(result.to_dict())
    for secret in ("PATIENT-NAME", "CLAIM-REASON-TEXT", PASSWORD, str(ME), str(OTHER), "signatory@example.test"):
        assert secret not in text and secret not in blob


# ── the filesystem is unchanged ───────────────────────────────────────────────────────────────────────────────────


def _snapshot(directory):
    out = {}
    for root, _dirs, files in os.walk(directory):
        for name in files:
            p = os.path.join(root, name)
            out[os.path.relpath(p, directory)] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
    return out


@pytest.mark.parametrize("name", sorted(OUTCOMES))
def test_status_leaves_the_run_directory_exactly_as_it_found_it(tmp_path, name):
    run_kwargs, answers, _ = OUTCOMES[name]
    run = linked_run(tmp_path, **dict(run_kwargs, journal=False))
    before = _snapshot(run)
    status_of(run, StandIn(answers))
    assert _snapshot(run) == before  # same names, same bytes, same modification times
    assert not any("journal" in n for n in os.listdir(run))
    assert not any(n.endswith(".log") for n in os.listdir(run) if n not in before)


@pytest.mark.parametrize("name", ["service_unreachable", "login_refused", "answer_is_not_json"])
def test_a_failed_read_also_writes_nothing(tmp_path, name):
    run = linked_run(tmp_path, **SIGNED)
    before = _snapshot(run)
    status_of(run, UNREADABLE[name][0]())
    assert _snapshot(run) == before


def test_status_is_deterministic_for_the_same_answers(tmp_path):
    run = linked_run(tmp_path, **SIGNED)
    results = [status_of(run, StandIn([(200, body("approved", approver=ME))])).to_dict() for _ in range(3)]
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("identity", [None, str(ME), "someone"])
def test_a_session_without_an_established_operator_identity_is_never_attributed(tmp_path, identity):
    """The operator is whoever the service authenticated, as a UUID. Anything else (nothing, a string that merely looks right,
    garbage) must not be read as 'you' or as 'another operator'."""

    class Access:
        def login(self, email, org_id, password, totp_code=None):
            return SimpleNamespace(user_id=identity, org_id=ORG)

        def get_signoff_state(self, session, report_id):
            from review.clinical_http import _parse_signoff_state

            return _parse_signoff_state(body("approved", approver=ME), REPORT)

    result = st.run_status(str(linked_run(tmp_path, **SIGNED)), clinical_credentials=CREDS, data_access=Access())
    assert (result.group, result.code, result.exit_code) == ("UNREADABLE", "OPERATOR_IDENTITY_UNAVAILABLE", 30)
