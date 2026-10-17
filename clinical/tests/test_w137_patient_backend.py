"""
clinical/tests/test_w137_patient_backend.py
───────────────────────────────────────────
w137 — the patient backend audit's two confirmed, in-scope findings, proved
behaviourally.

THE TWO THINGS THIS FILE IS ABOUT, and they are different shapes:

  1. THE COVERAGE GAP. w133 built patient credential revocation and proved it
     with 61 rows, and COUNTED at 4213cd3 not one of those rows touches a
     route: `grep -l TestClient clinical/tests/*.py` does not list any
     `test_w133_*` file. So "a revoked patient's live session is refused" was
     proved of `patient_session_valid` and never of `GET /portal/me`, which is
     where a revoked patient actually arrives. Nothing was broken; nothing was
     tested either, and those are different findings. TestARevokedPatient... is
     that half.

  2. THE DEFECT. `@auditable` binds an audit row's `org_id` from a STAFF
     `Session` and from nothing else, so `terminate_my_patient_session` --
     declared `requires_session=False` because the decorator refuses a
     `PatientSession` where it is True -- wrote every `patient_session_logged
     _out` row with `org_id = NULL`. `read_audit` is Auditor-gated and filters
     `WHERE org_id = %s`. A patient session termination was therefore recorded
     where the organisation that owns it cannot look.
     TestThePatientLogoutIsAuditedInItsOwnOrganisation is that half.

     The previous wave pinned the NULL deliberately rather than hiding it, and
     the pin's own failure message says what to do about it: "the decorator now
     populates org_id for a patient-principal method -- good, and this test's
     docstring is out of date" (test_w132_logout.py). That row is rewritten,
     not deleted, in the same commit as the fix.

RULE 2a — THE METHOD, NOT A RAW QUERY. Every visibility claim in this file is
made through `DataAccess.read_audit`, the Auditor-gated reader a real
organisation would use. Nothing here concludes "the organisation can see it"
from a `SELECT` against `audit_log`. That distinction is the whole point: a
direct SELECT sees rows with `org_id IS NULL` perfectly well, so a test built
on one passes against the broken version. (Precedent: the Phase 5d
cannot-fail-test incident.)

RULE 4 — EVERY ABSENCE HAS A POSITIVE TWIN. "Organisation B's auditor cannot
see it" is satisfied by a change that files nothing anywhere, which is the
broken state. The twin is that organisation B's auditor DOES see its own
patient's logout, and A's auditor does not see that one. Neither row may be
deleted without the other.

RULE 8 — AN ORACLE CAN LIVE IN EITHER CHANNEL. The portal refusal row asserts
`(status_code, content)` as a SET OF SIZE ONE across every refusal case,
rather than case-by-case equality, so a leak in a case nobody listed still
fails.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import uuid

import pytest
from clinical.data_access import session_audit_ref

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
PASSWORD = "correct horse battery staple"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"
CLIENT_IP = "203.0.113.7"

THE_REFUSAL = (401, json.dumps({"detail": "Authentication failed."}, separators=(",", ":")).encode())

PORTAL_READ_ROUTES = ("/portal/me", "/portal/me/tests")


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def clinical_dsn(monkeypatch):
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")

    from clinical.bootstrap import apply_schema, ensure_roles

    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    ensure_roles(connection, app_password=APP_PASSWORD, retention_password=RETENTION_PASSWORD)
    apply_schema(connection)
    connection.commit()
    connection.close()

    monkeypatch.setenv("CLINICAL_DSN", DSN)
    return DSN


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    """Ten attempts per source per minute is the shipped limit; this file logs
    in over HTTP more than ten times in some tests, and a 429 is not the
    refusal any of them is about."""
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    yield
    _LOGIN_THROTTLE.reset()


@pytest.fixture()
def conn(clinical_dsn):
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    from clinical.data_access import BcryptHasher, DataAccess

    return DataAccess(conn, password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def world(dao, conn):
    """
    TWO organisations, each with a staff account holding BOTH Administrator
    and Auditor, and two enrolled patients.

    Two of everything for the reason w132's own fixture gives: every question
    in this file is "did it reach the RIGHT one", and one of anything cannot
    answer it. Auditor as well as Administrator because the visibility claims
    here are made through `read_audit`, which is Auditor-gated -- the
    capability that asks the question is not the capability that performs the
    act, and both are needed in the same test.

    THE SAME EMAIL EXISTS IN BOTH ORGANISATIONS (`patient@shared.example`).
    `patient_logins_org_email_unique` is scoped to the organisation, so an
    address is unique only within one, and a tenant-isolation test built on
    two distinct addresses proves nothing about the axis -- it would pass
    against a lookup keyed on the address alone.
    """
    orgs = {}
    for tag, name in (("a", "Org A"), ("b", "Org B")):
        org_id = dao.create_organisation(name)
        staff_id = dao.create_user(org_id, f"admin@{tag}.example", PASSWORD)
        with conn.cursor() as cur:
            # w162: "Orderer" joins the pair because this account registers the
            # two patients below, and registration is now the ordering
            # clinician's (human ruling 2026-09-19). Roles are additive and
            # this file's claims are about the PATIENT axis, not staff roles.
            for role in ("Administrator", "Auditor", "Orderer"):
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (staff_id, org_id, role, staff_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
                )
        conn.commit()
        staff = dao.login(f"admin@{tag}.example", org_id, PASSWORD)

        first = dao.create_patient(staff, f"First {tag.upper()}", dt.date(1990, 1, 1), "F")
        second = dao.create_patient(staff, f"Second {tag.upper()}", dt.date(1985, 5, 5), "M")
        first_login = dao.enrol_patient_login(staff, first, "patient@shared.example", PASSWORD)
        second_login = dao.enrol_patient_login(staff, second, f"second@{tag}.example", PASSWORD)

        orgs[tag] = {
            "org_id": org_id,
            "staff_id": staff_id,
            "staff": staff,
            "first_patient": first,
            "second_patient": second,
            "first_login": first_login,
            "second_login": second_login,
            "first_email": "patient@shared.example",
            "second_email": f"second@{tag}.example",
        }
    conn.commit()
    return orgs


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678))


def _portal_login(client, org_id, email, password=PASSWORD):
    """Authenticate over HTTP and return the opaque session id the browser got.

    Through the real route, because a cookie minted by calling
    `DataAccess.patient_login` in-process is not the credential a browser
    holds, and this file's subject is what happens at the route.
    """
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post(
        "/portal/login",
        json={"email": email, "org_id": str(org_id), "password": password},
    )
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _staff_login(client, org_id, email, password=PASSWORD):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post(
        "/auth/login",
        json={"email": email, "org_id": str(org_id), "password": password},
    )
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _audited_logouts(dao, staff_session):
    """
    Every `patient_session_logged_out` entry THIS ORGANISATION CAN SEE, read
    through the Auditor-gated reader rather than out of the table.

    read_audit returns (log_id, org_id, user_id, action, details, ip_address,
    timestamp); this returns (org_id, user_id, details) per row.
    """
    rows = dao.read_audit(staff_session, actions=["patient_session_logged_out"])
    return [(r[1], r[2], r[4]) for r in rows]


# ─── 1. a revoked patient at the route, and the patient who was not revoked ──


class TestARevokedPatientIsRefusedAtTheRoute:
    """
    w133 proved this of `patient_session_valid`. COUNTED at 4213cd3: zero of
    its 61 rows exercises a route. This class is that proof at the boundary a
    revoked patient actually arrives at.
    """

    def test_the_patient_reads_her_own_record_before_the_revocation(self, client, world):
        """
        THE ANCHOR. Without it every refusal below is satisfied by a portal
        that never worked, and the whole class passes against a broken route.
        """
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json()["patient_id"] == str(world["a"]["first_patient"])
        assert response.json()["name"] == "First A"

    def test_the_revoked_credentials_live_session_is_refused_at_every_portal_route(self, client, dao, world):
        """
        Revocation goes through the real Administrator-gated method; the
        refusal is observed at the real routes, status AND body together.
        """
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: cookie}).status_code == 200

        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["first_login"], True)

        for route in PORTAL_READ_ROUTES:
            after = client.get(route, cookies={PATIENT_COOKIE: cookie})
            assert (after.status_code, after.content) == THE_REFUSAL, (route, after.status_code, after.content)
        logout = client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})
        assert (logout.status_code, logout.content) == THE_REFUSAL, (logout.status_code, logout.content)

    def test_the_positive_twin_the_unrelated_patients_session_still_answers(self, client, dao, world):
        """
        THE ROW THAT DOES THE OPPOSITE JOB. The row above is an ABSENCE
        assertion and a revocation that terminated every patient session in
        the organisation satisfies it completely. This is the only row here
        that catches that, and it is asserted on CONTENT -- the second
        patient's own id and name -- not on "did not raise", because a 200
        carrying the wrong patient is the failure that matters most.

        NEITHER ROW MAY BE DELETED WITHOUT THE OTHER.
        """
        revoked = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        bystander = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        assert revoked != bystander

        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["first_login"], True)

        response = client.get("/portal/me", cookies={PATIENT_COOKIE: bystander})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["patient_id"] == str(world["a"]["second_patient"]), body
        assert body["name"] == "Second A", body
        assert body["patient_id"] != str(world["a"]["first_patient"]), body

    def test_the_revoked_patient_cannot_authenticate_again_over_http(self, client, dao, world):
        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["first_login"], True)
        response = client.post(
            "/portal/login",
            json={
                "email": world["a"]["first_email"],
                "org_id": str(world["a"]["org_id"]),
                "password": PASSWORD,
            },
        )
        assert (response.status_code, response.content) == THE_REFUSAL, response.text

    def test_the_positive_twin_the_unrelated_patient_can_still_authenticate(self, client, dao, world):
        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["first_login"], True)
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json()["patient_id"] == str(world["a"]["second_patient"])

    def test_every_portal_refusal_is_one_pair_and_the_revoked_one_is_in_it(self, client, dao, world):
        """
        RULE 8, as a SET of size one over both channels. A per-case equality
        passes against a leak in a case nobody listed; a set of size one does
        not. The revoked session is in the set deliberately -- that is the new
        case, and the point is that it is indistinguishable from an id that
        never existed.
        """
        revoked = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        logged_out_cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: logged_out_cookie})
        staff_cookie = _staff_login(client, world["a"]["org_id"], "admin@a.example")
        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["first_login"], True)

        cases = {
            "no credential at all": {},
            "a malformed id": {PATIENT_COOKIE: "not-a-uuid"},
            "a well-formed id that names nothing": {PATIENT_COOKIE: str(uuid.uuid4())},
            "a genuine STAFF session id": {PATIENT_COOKIE: staff_cookie},
            "a session ended by logout": {PATIENT_COOKIE: logged_out_cookie},
            "a session ended by revocation": {PATIENT_COOKIE: revoked},
        }
        assert len(cases) == 6, "the case list shrank -- this row would prove less than it claims"

        observed = {name: None for name in cases}
        for name, cookies in cases.items():
            response = client.get("/portal/me", cookies=cookies)
            observed[name] = (response.status_code, response.content)

        assert len(set(observed.values())) == 1, observed
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_the_positive_twin_a_live_patient_session_is_not_in_that_set(self, client, world):
        """Without this, a portal that refused EVERYTHING would satisfy the
        set-of-size-one row perfectly."""
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert (response.status_code, response.content) != THE_REFUSAL
        assert response.status_code == 200, response.text


# ─── 2. the two axes at the route: tenant, and patient ───────────────────────


class TestTheTwoAxesAtTheRoute:
    """
    Tenant isolation and patient isolation are SEPARATE axes and this class
    keeps them apart. The same address exists in both organisations, so an
    implementation that bound only the address -- or only the organisation --
    fails a row here.
    """

    def test_one_address_in_two_organisations_reaches_two_different_patients(self, client, world):
        """
        THE TENANT AXIS, through the real login route. Same email, same
        password, different `org_id` -- and the two sessions must reach two
        different patient records, asserted on content.
        """
        a_cookie = _portal_login(client, world["a"]["org_id"], "patient@shared.example")
        b_cookie = _portal_login(client, world["b"]["org_id"], "patient@shared.example")

        a_body = client.get("/portal/me", cookies={PATIENT_COOKIE: a_cookie}).json()
        b_body = client.get("/portal/me", cookies={PATIENT_COOKIE: b_cookie}).json()

        assert a_body["patient_id"] == str(world["a"]["first_patient"]), a_body
        assert b_body["patient_id"] == str(world["b"]["first_patient"]), b_body
        assert a_body["patient_id"] != b_body["patient_id"]
        assert a_body["name"] == "First A" and b_body["name"] == "First B"

    def test_a_patient_session_from_one_organisation_never_names_the_others_patient(self, client, world):
        """
        THE PATIENT AXIS ACROSS THE TENANT ONE. Organisation B's patient id is
        supplied in the query string, the body and a header while holding
        organisation A's cookie. All three channels, because the parameter
        does not exist and the proof must be that supplying it anyway changes
        nothing.
        """
        a_cookie = _portal_login(client, world["a"]["org_id"], "patient@shared.example")
        foreign = str(world["b"]["first_patient"])

        attempts = [
            client.get(f"/portal/me?patient_id={foreign}", cookies={PATIENT_COOKIE: a_cookie}),
            client.request("GET", "/portal/me", json={"patient_id": foreign}, cookies={PATIENT_COOKIE: a_cookie}),
            client.get("/portal/me", headers={"X-Patient-Id": foreign}, cookies={PATIENT_COOKIE: a_cookie}),
        ]
        assert len(attempts) == 3
        for response in attempts:
            assert response.status_code == 200, response.text
            assert response.json()["patient_id"] == str(world["a"]["first_patient"]), response.text

    def test_a_credential_does_not_authenticate_against_the_other_organisation(self, client, world):
        """The address exists in BOTH organisations, so this row is about the
        org predicate in the lookup and not about an unknown address."""
        response = client.post(
            "/portal/login",
            json={
                "email": world["a"]["second_email"],
                "org_id": str(world["b"]["org_id"]),
                "password": PASSWORD,
            },
        )
        assert (response.status_code, response.content) == THE_REFUSAL, response.text

    def test_the_positive_twin_the_same_credential_in_its_own_organisation_works(self, client, world):
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: cookie}).status_code == 200

    def test_staff_cannot_revoke_across_organisations_and_the_live_session_survives(self, client, dao, world):
        """
        STAFF CROSS-ORGANISATION, through the real Administrator-gated
        operation, with the effect observed at the route.

        w133 proves the refusal and proves the credential still authenticates.
        This adds the half a patient actually experiences: the LIVE session
        the cross-organisation administrator tried to end is still serving.
        A revocation whose org predicate was missing would refuse nothing and
        end the session anyway, and only this row would notice.
        """
        from clinical.data_access import AuthorizationError, NotFoundError

        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: cookie}).status_code == 200

        with pytest.raises((NotFoundError, AuthorizationError)):
            dao.set_patient_login_disabled(world["b"]["staff"], world["a"]["second_login"], True)

        after = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert after.status_code == 200, after.text
        assert after.json()["patient_id"] == str(world["a"]["second_patient"]), after.text

    def test_the_positive_twin_the_organisations_own_administrator_can(self, client, dao, world):
        """Without this, a `set_patient_login_disabled` that refused EVERY
        caller would pass the row above."""
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["second_email"])
        dao.set_patient_login_disabled(world["a"]["staff"], world["a"]["second_login"], True)
        after = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert (after.status_code, after.content) == THE_REFUSAL, after.text


# ─── 3. the audit defect: the row lands in its own organisation ──────────────


class TestThePatientLogoutIsAuditedInItsOwnOrganisation:
    """
    EVERY CLAIM HERE IS MADE THROUGH `read_audit`, the Auditor-gated reader
    (Rule 2a). A raw `SELECT * FROM audit_log` sees a row with
    `org_id IS NULL` perfectly well, so a test written that way passes against
    the broken version -- which is precisely how this gap survived a wave that
    already had an audit test for the same method.
    """

    def test_the_organisations_own_auditor_finds_the_patient_logout(self, client, dao, world):
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie}).status_code == 204

        visible = _audited_logouts(dao, world["a"]["staff"])
        assert len(visible) == 1, visible
        org_id, user_id, details = visible[0]
        assert org_id == world["a"]["org_id"], visible
        # w173: a REFERENCE, never the session id (which is the portal cookie)
        assert details["session_ref"] == session_audit_ref(cookie), visible
        assert "session_id" not in details and cookie not in repr(visible), visible

    def test_the_row_names_no_staff_user_because_a_patient_is_not_one(self, client, dao, world):
        """
        `audit_log.user_id` means a `users` row. A patient is not one, and
        writing the `patient_login_id` there would be a false claim on the
        column `audit_org_user` is indexed by. The acting credential belongs in
        `details`, where it already is.
        """
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})

        org_id, user_id, details = _audited_logouts(dao, world["a"]["staff"])[0]
        assert user_id is None, (user_id, details)
        assert details["patient_login_id"] == str(world["a"]["first_login"]), details

    def test_the_positive_twin_a_staff_logout_row_does_carry_its_user(self, client, dao, world):
        """
        Without this row, "user_id is None" is satisfied by a decorator that
        stopped populating `user_id` for anybody -- an absence assertion
        passing because the whole column went dark.
        """
        staff_cookie = _staff_login(client, world["a"]["org_id"], "admin@a.example")
        assert client.post("/auth/logout", cookies={STAFF_COOKIE: staff_cookie}).status_code == 204

        rows = dao.read_audit(world["a"]["staff"], actions=["session_logged_out"])
        assert len(rows) == 1, rows
        assert rows[0][1] == world["a"]["org_id"], rows
        assert rows[0][2] == world["a"]["staff_id"], rows

    def test_another_organisations_auditor_does_not_find_it(self, client, dao, world):
        """
        THE ABSENCE HALF. On its own it is satisfied by the broken version --
        a NULL org_id is invisible to EVERY organisation, including the right
        one. It is load-bearing only against the opposite failure: a binding
        that files every patient logout under one organisation, or under the
        wrong one. Paired below.
        """
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})

        assert _audited_logouts(dao, world["b"]["staff"]) == []

    def test_the_positive_twin_each_organisation_sees_exactly_its_own(self, client, dao, world):
        """
        THE PAIR. Both organisations' patients log out; each auditor must see
        exactly one row and it must be their own, asserted on the session id
        in `details` rather than on a count. A count of one is satisfied by
        two rows filed under one organisation as easily as by the right one.

        NEITHER ROW MAY BE DELETED WITHOUT THE OTHER.
        """
        a_cookie = _portal_login(client, world["a"]["org_id"], "patient@shared.example")
        b_cookie = _portal_login(client, world["b"]["org_id"], "patient@shared.example")
        assert a_cookie != b_cookie
        client.post("/portal/logout", cookies={PATIENT_COOKIE: a_cookie})
        client.post("/portal/logout", cookies={PATIENT_COOKIE: b_cookie})

        a_visible = _audited_logouts(dao, world["a"]["staff"])
        b_visible = _audited_logouts(dao, world["b"]["staff"])

        assert [d["session_ref"] for _, _, d in a_visible] == [session_audit_ref(a_cookie)], a_visible
        assert [d["session_ref"] for _, _, d in b_visible] == [session_audit_ref(b_cookie)], b_visible
        assert a_cookie not in repr(a_visible) and b_cookie not in repr(b_visible)
        assert all(org == world["a"]["org_id"] for org, _, _ in a_visible), a_visible
        assert all(org == world["b"]["org_id"] for org, _, _ in b_visible), b_visible

    def test_the_refused_attempt_is_visible_too_and_it_names_the_staff_actor(self, dao, world):
        """
        THE DENIED PATH. `_require_patient_session` refuses a staff session at
        the patient logout method; the decorator writes a `denied` row for it.
        A fix applied only to the success path leaves that row NULL-filed, and
        a refused attempt to end somebody's session is exactly the event an
        auditor is looking for. Here the acting principal IS a staff user, so
        `user_id` must name them.
        """
        from clinical.data_access import AuthorizationError

        with pytest.raises(AuthorizationError):
            dao.terminate_my_patient_session(world["a"]["staff"])

        visible = _audited_logouts(dao, world["a"]["staff"])
        assert len(visible) == 1, visible
        org_id, user_id, _ = visible[0]
        assert org_id == world["a"]["org_id"], visible
        assert user_id == world["a"]["staff_id"], visible

    def test_the_details_still_carry_the_organisation_and_the_credential(self, client, dao, world):
        """
        A REGRESSION ROW, not a new claim. w132 put both facts in `details`
        because the columns were unavailable; the columns being populated is
        not a reason to remove them, and a later reader comparing the two is
        how a mismatch would ever be noticed.
        """
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})

        org_id, _, details = _audited_logouts(dao, world["a"]["staff"])[0]
        assert details["org_id"] == str(world["a"]["org_id"]), details
        assert details["org_id"] == str(org_id), (details, org_id)
        assert details["patient_login_id"] == str(world["a"]["first_login"]), details


# ─── 4. the tripwire: decision 7 was not widened to get here ─────────────────


class TestDecisionSevenWasNotWidened:
    """
    The cheap way to make the audit row carry an organisation is to let the
    decorator accept a `PatientSession` where `requires_session=True`. That
    would hand every ungated staff reader a patient principal that duck-types
    into its org predicate -- the measured leak w130 exists to stop
    (`dao.get_patient(alice_session, bobs_patient_id)` RETURNED BOB'S RECORD).

    These rows fail the day somebody takes that route, and they are here, in
    the file that changes the decorator, rather than only in w130's suite.
    """

    def test_a_patient_session_is_still_refused_by_a_staff_reader(self, dao, world):
        from clinical.data_access import AuthorizationError

        patient_session = dao.patient_login(world["a"]["first_email"], world["a"]["org_id"], PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.get_patient(patient_session, world["a"]["second_patient"])

    def test_the_positive_twin_a_staff_session_reads_the_same_record(self, dao, world):
        record = dao.get_patient(world["a"]["staff"], world["a"]["second_patient"])
        assert record is not None
        assert record["name"] == "Second A", record

    def test_a_patient_session_is_still_refused_by_a_staff_write(self, dao, world):
        from clinical.data_access import AuthorizationError

        patient_session = dao.patient_login(world["a"]["first_email"], world["a"]["org_id"], PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.enrol_patient_login(patient_session, world["a"]["second_patient"], "new@a.example", PASSWORD)

    def test_a_patient_session_still_carries_no_user_id(self, dao, world):
        """
        Structural, and the reason it is worth a row: the audit binding added
        by this wave reads `user_id` off the acting principal with `getattr`,
        and it returns None for a patient only because the dataclass genuinely
        has no such field. If somebody adds one, `user_id` silently starts
        carrying a patient id into a staff actor column.
        """
        session = dao.patient_login(world["a"]["first_email"], world["a"]["org_id"], PASSWORD)
        assert hasattr(session, "patient_id"), "wrong object -- this guard would pass vacuously"
        assert hasattr(session, "org_id"), "wrong object -- this guard would pass vacuously"
        assert not hasattr(session, "user_id"), (
            "PatientSession has acquired a user_id; the audit binding in @auditable would now write "
            "a patient identifier into audit_log.user_id, which means a users row"
        )
