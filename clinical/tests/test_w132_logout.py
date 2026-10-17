"""
clinical/tests/test_w132_logout.py
──────────────────────────────────
w132c — SESSION TERMINATION, for both principals, through the method and
through the route.

THE STATE BEFORE THIS FILE. `sessions.terminated_at` and
`patient_sessions.terminated_at` were READ on every request and WRITTEN by
almost nothing: the only writer anywhere was `terminate_session`, which ends
SOMEBODY ELSE'S session and is Administrator-gated. `patient_sessions` had no
writer at all. So a session could not be ended by the person holding it, in
either principal, and a browser that had logged in had no way to log out.

THE TWO WRONG VERSIONS THIS FILE WAS BUILT AGAINST, both written and run
before the right one (the counts are in the wave report):

  * TERMINATE BY ID WITHOUT CHECKING OWNERSHIP — a `/auth/logout` that takes
    a `session_id` in the body and ends it, scoped only to the caller's
    organisation. Every "I logged out" row passes. What fails is
    `TestOneSessionAndOnlyOne`: a colleague's session id in the body ends the
    colleague's session.
  * TERMINATE ALL OF A PRINCIPAL'S SESSIONS — the patient version that ends
    every row for the credential rather than the one presented. Every "I
    logged out" row passes here too, and it is worse than the first: nothing
    in a naive suite fails at all, because terminating too much looks exactly
    like terminating enough.

THE POSITIVE TWIN IS THEREFORE THE LOAD-BEARING HALF OF THIS FILE, and it is
a Standing Rule 4 instance in its purest form: "the session is now refused"
is an ABSENCE assertion, and a logout that terminated the entire table
satisfies every one of them. `TestOtherSessionsSurvive` is what the absence
assertions are paired with.

REFUSALS ARE PROVED BY CALLING THE METHOD OR HITTING THE ROUTE (Standing Rule
2a). No test here concludes "the session is dead" from a `SELECT` on
`terminated_at`. The `SELECT`s that do appear assert the VALUE of
`terminated_by` — that it is the EXPECTED id, not merely non-NULL — which is
a different claim and one only a read can make.
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
    TWO organisations, and in the first one two staff accounts and two
    patients. Two of everything, because every interesting question in this
    file is "did it end the RIGHT one", and one of anything cannot answer it.

    The second staff account holds no role at all. That is deliberate:
    `terminate_session` is Administrator-gated, and a self-logout that
    inherited that gate would mean an ordinary user could not log out. The
    role-free account is what proves it did not.
    """
    from datetime import date

    org_a = dao.create_organisation("Org A")
    org_b = dao.create_organisation("Org B")

    admin_id = dao.create_user(org_a, "admin@a.example", PASSWORD)
    plain_id = dao.create_user(org_a, "plain@a.example", PASSWORD)
    b_user_id = dao.create_user(org_b, "user@b.example", PASSWORD)

    with conn.cursor() as cur:
        # w162. ORDERER ALONGSIDE ADMINISTRATOR, because this account registers
        # the two patients below and registration is now the ordering
        # clinician's (human ruling 2026-09-19). Roles are additive and nothing
        # here is relaxed: the account whose refusals this file asserts is
        # `plain@a.example`, which still holds no role at all.
        for role in ("Administrator", "Orderer"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (admin_id, org_a, role, admin_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
            )
    conn.commit()

    admin_session = dao.login("admin@a.example", org_a, PASSWORD)

    alice_id = dao.create_patient(admin_session, "Alice", date(1990, 1, 1), "F")
    bob_id = dao.create_patient(admin_session, "Bob", date(1985, 5, 5), "M")
    alice_login_id = dao.enrol_patient_login(admin_session, alice_id, "alice@a.example", PASSWORD)
    bob_login_id = dao.enrol_patient_login(admin_session, bob_id, "bob@a.example", PASSWORD)

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_id": admin_id,
        "plain_id": plain_id,
        "b_user_id": b_user_id,
        "alice_id": alice_id,
        "bob_id": bob_id,
        "alice_login_id": alice_login_id,
        "bob_login_id": bob_login_id,
    }


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678))


def _termination(conn, table, session_id):
    """
    The RECORDED terminating actor and stamp.

    This is a read, and it is here for the one question a read is the only way
    to answer: WHO is recorded as having ended it. "Not NULL" would be
    satisfied by a logout that wrote the wrong id — the schema CHECK
    (`terminated_at IS NULL) = (terminated_by IS NULL`) guarantees the column
    is populated whenever the stamp is, so non-NULL proves nothing beyond the
    CHECK. Every test that uses this compares against an EXPECTED id.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT terminated_at, terminated_by FROM {table} WHERE session_id = %s",  # noqa: S608 - fixed literals
            (session_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def _method_source(name):
    """
    (parameter names, body source) for one `DataAccess` method, read from the
    file that ships.

    ANCHORED: the path is resolved against THIS file and every step of it is
    asserted, because a structural read that lands nowhere returns nothing and
    a test built on nothing passes.
    """
    import ast

    path = pathlib.Path(__file__).resolve().parent.parent / "data_access.py"
    assert path.is_file(), f"{path} does not exist -- this guard is reading the wrong tree"
    source = path.read_text(encoding="utf-8")
    assert source.strip(), f"{path} is empty"

    tree = ast.parse(source)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]
    assert classes, f"no class DataAccess in {path}"
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert methods, f"no method {name!r} on DataAccess in {path}"

    node = methods[0]
    return [a.arg for a in node.args.args], ast.unparse(node)


def _audit(conn, action):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, outcome, user_id, org_id FROM audit_log WHERE action = %s ORDER BY log_id",
            (action,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


# ─── the staff side, through the method ──────────────────────────────────────


class TestAStaffSessionCanEndItself:
    def test_the_session_is_refused_afterwards_by_the_validator(self, dao, world):
        """
        Standing Rule 2a: the refusal is proved by CALLING `session_valid`,
        which is the function every request actually goes through. A `SELECT`
        on `terminated_at` would prove the column was written, not that the
        code reads it.
        """
        from clinical.data_access import SessionExpired

        session = dao.login("plain@a.example", world["org_a"], PASSWORD)
        assert dao.session_valid(session.session_id).user_id == world["plain_id"]

        dao.terminate_my_session(session)

        with pytest.raises(SessionExpired):
            dao.session_valid(session.session_id)

    def test_the_terminating_actor_is_the_user_who_logged_out(self, dao, conn, world):
        """The EXPECTED id, not merely non-NULL — see `_termination`."""
        session = dao.login("plain@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_session(session)

        terminated_at, terminated_by = _termination(conn, "sessions", session.session_id)
        assert terminated_at is not None
        assert terminated_by == world["plain_id"], (
            f"terminated_by is {terminated_by!r}, expected the logging-out user {world['plain_id']!r}"
        )

    def test_an_ordinary_user_with_no_role_can_log_out(self, dao, world):
        """
        `terminate_session` requires Administrator. A self-logout that reused
        it, or copied its `_require_role`, would mean the only people who
        could log out were administrators — a defect that looks like security.
        `plain@a.example` holds no role assignment at all.
        """
        from clinical.data_access import SessionExpired

        session = dao.login("plain@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_session(session)
        with pytest.raises(SessionExpired):
            dao.session_valid(session.session_id)

    def test_the_twin_terminate_session_still_requires_administrator(self, dao, world):
        """
        The existing authorisation was NOT weakened. Without this row, the one
        above is satisfied by a wave that deleted `_require_role` from
        `terminate_session` as well.
        """
        from clinical.data_access import AuthorizationError

        plain = dao.login("plain@a.example", world["org_a"], PASSWORD)
        victim = dao.login("admin@a.example", world["org_a"], PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.terminate_session(plain, victim.session_id)

    def test_logging_out_twice_does_not_rewrite_the_record(self, dao, conn, world):
        """
        The UPDATE is `... AND terminated_at IS NULL`. Without that clause a
        second call moves the timestamp, so the audit record of WHEN the
        session ended would be the time of the last redundant call.
        """
        session = dao.login("plain@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_session(session)
        first = _termination(conn, "sessions", session.session_id)

        dao.terminate_my_session(session)
        assert _termination(conn, "sessions", session.session_id) == first


class TestAPatientSessionCanEndItself:
    def test_the_session_is_refused_afterwards_by_the_validator(self, dao, world):
        from clinical.data_access import SessionExpired

        session = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        assert dao.patient_session_valid(session.session_id).patient_id == world["alice_id"]

        dao.terminate_my_patient_session(session)

        with pytest.raises(SessionExpired):
            dao.patient_session_valid(session.session_id)

    def test_the_terminating_actor_is_the_patient_login_that_logged_out(self, dao, conn, world):
        """
        `terminated_by` holds the PATIENT_LOGIN_ID — the credential that
        performed the act — and this is expressible only because
        `terminated_by` carries NO foreign key on either session table. Had it
        referenced `users`, patient self-logout would have needed a schema
        change. It does not, and `TestNoSchemaChangeWasNeeded` pins that.
        """
        session = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_patient_session(session)

        terminated_at, terminated_by = _termination(conn, "patient_sessions", session.session_id)
        assert terminated_at is not None
        assert terminated_by == world["alice_login_id"], (
            f"terminated_by is {terminated_by!r}, expected alice's patient_login_id {world['alice_login_id']!r}"
        )

    def test_logging_out_twice_does_not_rewrite_the_record(self, dao, conn, world):
        session = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_patient_session(session)
        first = _termination(conn, "patient_sessions", session.session_id)

        dao.terminate_my_patient_session(session)
        assert _termination(conn, "patient_sessions", session.session_id) == first


# ─── THE POSITIVE TWIN ───────────────────────────────────────────────────────


class TestOtherSessionsSurvive:
    """
    STANDING RULE 4, AND THE REASON THIS CLASS EXISTS. Every assertion above
    is an ABSENCE — "the session no longer validates" — and an absence is
    satisfied by a logout that terminated everything. `UPDATE sessions SET
    terminated_at = now()` with no WHERE clause at all passes every row in
    this file except these.

    Each row below names a DIFFERENT over-reach, because they fail
    differently: the same principal's second device, a different person in the
    same organisation, and a different organisation entirely.
    """

    def test_the_same_users_second_session_still_validates(self, dao, world):
        """
        The two-devices case, and the one the "terminate all my sessions"
        wrong version fails. Logging out of a phone must not log out the
        workstation — and if it should, that is a product ruling with a
        different method name, not the default behaviour of a logout button.
        """
        phone = dao.login("plain@a.example", world["org_a"], PASSWORD)
        desktop = dao.login("plain@a.example", world["org_a"], PASSWORD)
        assert phone.session_id != desktop.session_id

        dao.terminate_my_session(phone)

        assert dao.session_valid(desktop.session_id).user_id == world["plain_id"]

    def test_a_colleagues_session_in_the_same_organisation_still_validates(self, dao, world):
        colleague = dao.login("admin@a.example", world["org_a"], PASSWORD)
        mine = dao.login("plain@a.example", world["org_a"], PASSWORD)

        dao.terminate_my_session(mine)

        assert dao.session_valid(colleague.session_id).user_id == world["admin_id"]

    def test_a_session_in_another_organisation_still_validates(self, dao, world):
        elsewhere = dao.login("user@b.example", world["org_b"], PASSWORD)
        mine = dao.login("plain@a.example", world["org_a"], PASSWORD)

        dao.terminate_my_session(mine)

        assert dao.session_valid(elsewhere.session_id).org_id == world["org_b"]

    def test_the_same_patients_second_session_still_validates(self, dao, world):
        """The patient mirror, and the row the 'terminate all' wrong version
        was written to fail."""
        phone = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        desktop = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        assert phone.session_id != desktop.session_id

        dao.terminate_my_patient_session(phone)

        assert dao.patient_session_valid(desktop.session_id).patient_id == world["alice_id"]

    def test_another_patients_session_still_validates(self, dao, world):
        bob = dao.patient_login("bob@a.example", world["org_a"], PASSWORD)
        alice = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)

        dao.terminate_my_patient_session(alice)

        assert dao.patient_session_valid(bob.session_id).patient_id == world["bob_id"]

    def test_a_patient_logout_does_not_touch_staff_sessions(self, dao, world):
        """The two tables are separate and the two methods must stay in their
        own. A logout that wrote to both would be the dependency-widening the
        whole second-axis design refuses, arriving through the write path."""
        staff = dao.login("plain@a.example", world["org_a"], PASSWORD)
        alice = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)

        dao.terminate_my_patient_session(alice)

        assert dao.session_valid(staff.session_id).user_id == world["plain_id"]

    def test_a_staff_logout_does_not_touch_patient_sessions(self, dao, world):
        staff = dao.login("plain@a.example", world["org_a"], PASSWORD)
        alice = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)

        dao.terminate_my_session(staff)

        assert dao.patient_session_valid(alice.session_id).patient_id == world["alice_id"]


class TestOneSessionAndOnlyOne:
    """
    THE ROW THE FIRST WRONG VERSION FAILS. A logout that takes a session id
    and ends it, scoped only to the organisation, lets any authenticated user
    end any colleague's session — which is `terminate_session`'s
    Administrator-gated capability, handed to everybody through a route named
    "logout".

    The fix is not a check. It is that THERE IS NOTHING TO CHECK: the method
    takes no target, and the id it ends is read from the session object the
    dependency produced. A check can be forgotten; a missing parameter cannot.
    """

    def test_the_staff_method_accepts_no_target_session_id(self):
        """
        READ FROM THE AST OF `data_access.py`, NOT FROM `inspect.signature`,
        and the reason is a cannot-fail hazard in its own right: `@auditable`
        wraps every public method in a plain `def wrapper(self, *args,
        **kwargs)` with no `functools.wraps`, so `inspect.signature` reports
        `['self', 'args', 'kwargs']` for EVERY method in this layer. A test
        asserting on that signature would have been asserting on the
        decorator, identically, forever, whatever the method underneath
        declared.
        """
        params, body = _method_source("terminate_my_session")
        assert params == ["self", "session"], (
            f"terminate_my_session takes {params}; a logout that accepts an identifier is a "
            "logout that can end somebody else's session"
        )
        assert "session.session_id" in body, body

    def test_the_patient_method_accepts_no_target_session_id(self):
        params, body = _method_source("terminate_my_patient_session")
        assert params == ["self", "patient_session"], params
        assert "patient_session.session_id" in body, body
        assert "patient_login_id = %s" not in body, (
            "the UPDATE is keyed on the credential rather than on the session presented -- "
            "that logs the patient out of every device they hold"
        )

    def test_the_route_accepts_no_body_that_could_name_a_session(self, client, world, dao):
        """
        The structural rows above are about the method. This one is about the
        route, and it is the one an attacker would actually try: a body
        carrying somebody else's session id.
        """
        colleague = dao.login("admin@a.example", world["org_a"], PASSWORD)
        mine = dao.login("plain@a.example", world["org_a"], PASSWORD)

        response = client.post(
            "/auth/logout",
            json={"session_id": str(colleague.session_id)},
            cookies={STAFF_COOKIE: str(mine.session_id)},
        )
        assert response.status_code == 204, response.text

        # The colleague is untouched. Proved by CALLING the validator.
        assert dao.session_valid(colleague.session_id).user_id == world["admin_id"]


# ─── the staff side, through the route ───────────────────────────────────────


class TestTheStaffLogoutRoute:
    def _login(self, client, world):
        response = client.post(
            "/auth/login",
            json={"email": "plain@a.example", "org_id": str(world["org_a"]), "password": PASSWORD},
        )
        assert response.status_code == 204, response.text
        header = response.headers.get_list("set-cookie")[0]
        return header.split("=", 1)[1].split(";", 1)[0]

    def test_a_logged_in_staff_browser_can_log_out(self, client, world):
        session_id = self._login(client, world)
        response = client.post("/auth/logout", cookies={STAFF_COOKIE: session_id})
        assert response.status_code == 204, response.text
        assert response.content == b""

    def test_the_cookie_no_longer_authenticates_at_a_real_staff_route(self, client, world):
        """
        THE BEHAVIOURAL PROOF, and the only one that matters: the credential
        is replayed at a route that requires a session, through the real
        dependency. Status AND body together (Standing Rules 2b and 8), and
        against the same refusal constant every other anonymous refusal uses.
        """
        session_id = self._login(client, world)
        assert client.get("/exceptions/worklist?owner=x", cookies={STAFF_COOKIE: session_id}).status_code == 200

        client.post("/auth/logout", cookies={STAFF_COOKIE: session_id})

        after = client.get("/exceptions/worklist?owner=x", cookies={STAFF_COOKIE: session_id})
        assert (after.status_code, after.content) == THE_REFUSAL

    def test_the_twin_an_unrelated_browsers_cookie_still_works(self, client, world):
        """
        The positive twin at the HTTP layer. Two browsers, two logins; one
        logs out and the other must be unaffected. Without this row a logout
        that terminated the whole table passes the row above.
        """
        mine = self._login(client, world)
        theirs = self._login(client, world)
        assert mine != theirs

        client.post("/auth/logout", cookies={STAFF_COOKIE: mine})

        after = client.get("/exceptions/worklist?owner=x", cookies={STAFF_COOKIE: theirs})
        assert after.status_code == 200, after.text

    def test_the_response_clears_the_cookie_on_the_same_path_it_was_set(self, client, world):
        """
        Asserted on the RAW header. A cookie is keyed on (name, domain, path);
        a deletion sent with a different path adds a second empty cookie and
        leaves the live one in the jar. The set path for the staff cookie is
        `/`, so the clear must be `/` too.
        """
        set_header = client.post(
            "/auth/login",
            json={"email": "plain@a.example", "org_id": str(world["org_a"]), "password": PASSWORD},
        ).headers.get_list("set-cookie")[0]
        session_id = set_header.split("=", 1)[1].split(";", 1)[0]

        clear_headers = client.post("/auth/logout", cookies={STAFF_COOKIE: session_id}).headers.get_list("set-cookie")
        assert len(clear_headers) == 1, clear_headers
        header = clear_headers[0]
        assert header.startswith(f"{STAFF_COOKIE}="), header
        assert f"{STAFF_COOKIE}={session_id}" not in header, "the clear header echoes the live credential back"

        def path_of(raw):
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key.lower() == "path":
                    return value
            return None

        assert path_of(header) == path_of(set_header) == "/", (header, set_header)
        assert "expires=" in header.lower() or "max-age=0" in header.lower(), header

    def test_an_anonymous_caller_cannot_log_anybody_out(self, client, world):
        for cookies in ({}, {STAFF_COOKIE: "not-a-uuid"}, {STAFF_COOKIE: str(uuid.uuid4())}):
            response = client.post("/auth/logout", cookies=cookies)
            assert (response.status_code, response.content) == THE_REFUSAL, cookies

    def test_a_patient_cookie_is_not_a_staff_logout_credential(self, client, world, dao):
        """
        Cross-principal. A patient session id presented to the staff logout
        must be refused exactly as an unknown id is — and the patient's
        session must survive, which is what makes this more than a status
        check.
        """
        alice = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        response = client.post("/auth/logout", cookies={STAFF_COOKIE: str(alice.session_id)})
        assert (response.status_code, response.content) == THE_REFUSAL
        assert dao.patient_session_valid(alice.session_id).patient_id == world["alice_id"]


class TestThePatientLogoutRoute:
    def _login(self, client, world, email="alice@a.example"):
        response = client.post(
            "/portal/login",
            json={"email": email, "org_id": str(world["org_a"]), "password": PASSWORD},
        )
        assert response.status_code == 204, response.text
        header = response.headers.get_list("set-cookie")[0]
        return header.split("=", 1)[1].split(";", 1)[0]

    def test_a_logged_in_patient_browser_can_log_out(self, client, world):
        session_id = self._login(client, world)
        response = client.post("/portal/logout", cookies={PATIENT_COOKIE: session_id})
        assert response.status_code == 204, response.text
        assert response.content == b""

    def test_the_cookie_no_longer_authenticates_at_a_real_portal_route(self, client, world):
        session_id = self._login(client, world)
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: session_id}).status_code == 200

        client.post("/portal/logout", cookies={PATIENT_COOKIE: session_id})

        after = client.get("/portal/me", cookies={PATIENT_COOKIE: session_id})
        assert (after.status_code, after.content) == THE_REFUSAL

    def test_the_twin_another_patients_browser_still_works(self, client, world):
        alice = self._login(client, world, "alice@a.example")
        bob = self._login(client, world, "bob@a.example")

        client.post("/portal/logout", cookies={PATIENT_COOKIE: alice})

        after = client.get("/portal/me", cookies={PATIENT_COOKIE: bob})
        assert after.status_code == 200, after.text
        assert after.json()["name"] == "Bob"

    def test_the_twin_the_same_patients_other_device_still_works(self, client, world):
        phone = self._login(client, world)
        desktop = self._login(client, world)
        assert phone != desktop

        client.post("/portal/logout", cookies={PATIENT_COOKIE: phone})

        after = client.get("/portal/me", cookies={PATIENT_COOKIE: desktop})
        assert after.status_code == 200, after.text

    def test_the_response_clears_the_cookie_on_the_portal_path(self, client, world):
        set_header = client.post(
            "/portal/login",
            json={"email": "alice@a.example", "org_id": str(world["org_a"]), "password": PASSWORD},
        ).headers.get_list("set-cookie")[0]
        session_id = set_header.split("=", 1)[1].split(";", 1)[0]

        headers = client.post("/portal/logout", cookies={PATIENT_COOKIE: session_id}).headers.get_list("set-cookie")
        assert len(headers) == 1, headers
        header = headers[0]
        assert header.startswith(f"{PATIENT_COOKIE}="), header

        def path_of(raw):
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key.lower() == "path":
                    return value
            return None

        assert path_of(header) == path_of(set_header) == "/portal", (header, set_header)
        assert "clinical_session=" not in header, "the portal logout clears the STAFF cookie"

    def test_an_anonymous_caller_cannot_log_anybody_out(self, client, world):
        for cookies in ({}, {PATIENT_COOKIE: "not-a-uuid"}, {PATIENT_COOKIE: str(uuid.uuid4())}):
            response = client.post("/portal/logout", cookies=cookies)
            assert (response.status_code, response.content) == THE_REFUSAL, cookies

    def test_a_staff_cookie_is_not_a_patient_logout_credential(self, client, world, dao):
        staff = dao.login("plain@a.example", world["org_a"], PASSWORD)
        response = client.post("/portal/logout", cookies={PATIENT_COOKIE: str(staff.session_id)})
        assert (response.status_code, response.content) == THE_REFUSAL
        assert dao.session_valid(staff.session_id).user_id == world["plain_id"]


# ─── the principals do not cross at the method layer either ──────────────────


class TestThePrincipalsDoNotCross:
    def test_a_patient_session_is_refused_by_the_staff_logout_method(self, dao, world):
        """
        The `@auditable` refusal (w130 decision 7). A PatientSession carries
        `org_id`, so it duck-types into anything that reads only that — which
        is what makes the refusal necessary rather than decorative.
        """
        from clinical.data_access import AuthorizationError

        alice = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.terminate_my_session(alice)

        assert dao.patient_session_valid(alice.session_id).patient_id == world["alice_id"]

    def test_a_staff_session_is_refused_by_the_patient_logout_method(self, dao, world):
        from clinical.data_access import AuthorizationError

        staff = dao.login("plain@a.example", world["org_a"], PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.terminate_my_patient_session(staff)

        assert dao.session_valid(staff.session_id).user_id == world["plain_id"]


# ─── the audit trail ─────────────────────────────────────────────────────────


class TestTheTerminationIsAudited:
    """
    The schema's own comment on `patient_sessions` says the audit trail
    records creation and termination, not use. Creation was recorded; there
    was no termination to record.
    """

    def test_the_staff_logout_records_the_session_it_ended(self, dao, conn, world):
        session = dao.login("plain@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_session(session)

        rows = _audit(conn, "session_logged_out")
        # w173: an audit row records a session REFERENCE, never the session id (the id is the
        # cookie, and audit_log is append-only).
        assert [(r[0], r[1]) for r in rows] == [(session_audit_ref(session.session_id), "success")], rows
        assert str(session.session_id) not in repr(rows), rows
        assert rows[0][2] == world["plain_id"], rows
        assert rows[0][3] == world["org_a"], rows

    def test_the_staff_logout_is_a_different_action_from_an_administrative_termination(self, dao, conn, world):
        """
        Two different events must not share one action name. An inspector
        reading `session_terminated` is reading administrative intervention;
        a person logging out is not that, and collapsing them would make the
        administrative signal unfindable in the noise of ordinary logouts.
        """
        admin = dao.login("admin@a.example", world["org_a"], PASSWORD)
        victim = dao.login("plain@a.example", world["org_a"], PASSWORD)
        dao.terminate_session(admin, victim.session_id)
        dao.terminate_my_session(admin)

        terminated = [r[0] for r in _audit(conn, "session_terminated")]
        logged_out = [r[0] for r in _audit(conn, "session_logged_out")]
        assert terminated == [session_audit_ref(victim.session_id)], terminated
        assert logged_out == [session_audit_ref(admin.session_id)], logged_out
        assert str(victim.session_id) not in repr(terminated) + repr(logged_out)
        assert str(admin.session_id) not in repr(terminated) + repr(logged_out)

    def test_the_patient_logout_records_the_session_it_ended(self, dao, conn, world):
        session = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_patient_session(session)

        rows = _audit(conn, "patient_session_logged_out")
        assert [(r[0], r[1]) for r in rows] == [(session_audit_ref(session.session_id), "success")], rows
        assert str(session.session_id) not in repr(rows), rows

    def test_the_patient_logout_records_its_organisation_and_credential_in_details(self, dao, conn, world):
        """
        REWRITTEN BY w137, DELIBERATELY AND IN ONE COMMIT WITH THE FIX.

        This row used to assert `org_id IS NULL` and carried a docstring
        explaining the gap: the `@auditable` decorator bound the audit row's
        `org_id`/`user_id` columns from a STAFF `Session` and from nothing
        else, so a patient-principal method could not populate them. Its
        failure message named its own successor — "the decorator now populates
        org_id for a patient-principal method -- good, and this test's
        docstring is out of date" — and w137 is that wave.

        The decorator now reads the organisation from the acting principal for
        `requires_session=False` methods. DECISION 7 IS UNTOUCHED: the refusal
        of a `PatientSession` is keyed on `requires_session=True`, and w130's
        suite plus `test_w137_patient_backend.py::TestDecisionSevenWasNotWidened`
        are the tripwires for it.

        `details` STILL CARRIES BOTH FACTS and that is not redundancy: it is
        how a mismatch between the column and the payload would ever be
        noticed. `user_id` stays NULL because a patient is not a `users` row.

        The org-scoped question — can the organisation's own Auditor actually
        FIND this row, through `read_audit` rather than through a raw SELECT —
        is asked in `test_w137_patient_backend.py`, because a SELECT here sees
        the row either way and cannot tell the two versions apart.
        """
        session = dao.patient_login("alice@a.example", world["org_a"], PASSWORD)
        dao.terminate_my_patient_session(session)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, user_id, details FROM audit_log "
                "WHERE action = 'patient_session_logged_out' ORDER BY log_id"
            )
            rows = cur.fetchall()
        conn.commit()

        assert len(rows) == 1, rows
        org_column, user_column, details = rows[0]
        assert org_column == world["org_a"], (
            "the patient logout row is filed under no organisation, or under the wrong one; "
            "read_audit is org-scoped, so the organisation that owns this session cannot find it"
        )
        assert user_column is None, (
            "audit_log.user_id means a users row and a patient is not one -- the acting credential "
            "belongs in details, not here"
        )
        assert details["org_id"] == str(world["org_a"]), details
        assert details["patient_login_id"] == str(world["alice_login_id"]), details


# ─── the schema did not change ───────────────────────────────────────────────


class TestNoSchemaChangeWasNeeded:
    """
    The whole of this wave rests on one reading: `terminated_by` carries NO
    foreign key on either session table, so a patient credential id is an
    acceptable value in it. Had it referenced `users (user_id, org_id)` —
    which `terminate_session` writing `session.user_id` there makes a
    reasonable assumption — patient self-logout would have needed a migration,
    and this repository has no migration mechanism and three schema changes
    already held unlanded.

    Read from `schema.sql` rather than asserted, because the claim is about
    the file and the file is what ships.
    """

    def test_terminated_by_has_no_foreign_key_on_either_session_table(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert schema.strip(), f"schema.sql at {SCHEMA_PATH} is empty -- this guard would pass vacuously"

        for table in ("sessions", "patient_sessions"):
            start = schema.index(f"CREATE TABLE {table} (")
            body = schema[start : schema.index(");", start)]
            assert "terminated_by" in body, table
            fk_lines = [line for line in body.splitlines() if "FOREIGN KEY" in line or "REFERENCES" in line]
            assert not any("terminated_by" in line for line in fk_lines), (table, fk_lines)

    def test_the_grant_already_permits_the_update(self):
        """
        The grant was written in anticipation of exactly this write: its own
        comment names "the terminated_at stamp". No grant change either.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert schema.strip(), f"schema.sql at {SCHEMA_PATH} is empty -- this guard would pass vacuously"

        # The whole GRANT statement, comment lines stripped, from the keyword
        # to its terminating semicolon: it spans ten lines and names its
        # tables on several of them, so a line-at-a-time match cannot see it.
        statements = []
        for start in range(len(schema)):
            if not schema.startswith("GRANT  SELECT, INSERT, UPDATE", start):
                continue
            body = schema[start : schema.index(";", start)]
            statements.append("\n".join(line for line in body.splitlines() if not line.lstrip().startswith("--")))

        assert statements, "no `GRANT SELECT, INSERT, UPDATE` to clinical_app found -- wrong file?"
        granted = [s for s in statements if "TO clinical_app" in s]
        assert granted, statements

        joined = "\n".join(granted)
        assert "sessions" in joined, joined
        assert "patient_sessions" in joined, joined
        # The negative half: the same grant must still NOT carry DELETE.
        assert "DELETE" not in joined, joined
