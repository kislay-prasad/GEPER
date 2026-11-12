"""
clinical/tests/test_w138_patient_portal_api.py
──────────────────────────────────────────────
w138 — the patient portal HTTP surface. Wave 4's three confirmed COVERAGE
findings, proved behaviourally at the routes.

NOTHING IN THIS FILE FIXES A DEFECT, AND THAT IS STATED FIRST SO THE FILE IS
NOT READ AS SOMETHING IT IS NOT. Every refusal asserted below is already
correct at `a931869`. What was missing is the assertion that HOLDS it there --
and Rule 8 is explicit that the holding form is a SET OF SIZE ONE over both
channels, because "a per-case equality passes against a leak in a case nobody
listed". Three cases were not listed:

  1. TWO OF `patient_login`'s SIX FAILURE REASONS. Counted from the method's
     own source: unknown_organisation, organisation_disabled,
     unknown_patient_login, locked_out, invalid_password,
     patient_login_disabled. `test_w132_login_refusal_parity.py::_login_probes`
     drives FOUR of them through `/portal/login` under a docstring claiming
     "all six", and the two it misses -- `locked_out` and
     `patient_login_disabled` -- are exactly the two that separate an
     UNAUTHORISED patient from a NONEXISTENT one. Its staff half carries both
     of its equivalents; the asymmetry is patient-only. The docstring is
     corrected in the same commit.
     TestTheLoginRefusalIsOneAnswerOverAllSixReasons is that half, and the
     list of six is DERIVED FROM THE SOURCE rather than typed out, so a
     seventh reason added later fails a row here instead of being missed.

  2. AN EXPIRED PATIENT SESSION, AT ANY ROUTE, OF EITHER KIND. COUNTED across
     clinical/tests at a931869: the only patient-session expiry row anywhere is
     test_patient_identity.py::test_the_idle_window_expires_at_exactly_the_
     timeout, which calls the METHOD. Absolute expiry of a patient session is
     asserted at no layer at all. Wave 4 names "expired/terminated session";
     terminated is covered twice (logout, revocation) and expired nowhere.
     THE HAZARD IS A STATUS-CHANNEL ONE AND IT IS ONE WORD WIDE: all three
     expiry conditions raise `SessionExpired`, and `_get_patient_session`
     catches `AuthenticationError`. If `SessionExpired` ever stops inheriting
     from it, an expired session answers 500 where an unknown one answers 401
     -- an oracle confirming that an id names a real, once-valid session.
     TestAnExpiredPatientSessionIsRefusedAtEveryPortalRoute is that half.

  3. THE SET RAN OVER ONE ROUTE. Both existing set-of-size-one rows
     (test_w137_patient_backend.py:316, test_portal_endpoints_http.py:271)
     collect over `GET /portal/me` alone. The three authenticated handlers do
     NOT catch identically in source -- get_my_record catches (NotFoundError,
     AuthorizationError), list_my_tests and patient_logout catch
     AuthorizationError only -- so an oracle introduced on /portal/me/tests or
     /portal/logout is in no set at this SHA.
     TestEveryPortalRefusalIsOnePairOnEveryRoute is that half.

RULE 2a -- THE ROUTE, NOT A RAW QUERY. Every refusal and every success below is
observed at a real route through `TestClient`. Direct SQL appears only where a
FIXTURE must reach a state the clock cannot be asked for (a session past its
window); no assertion reads the database. The distinction is the Phase 5d
cannot-fail-test incident: a test that builds its expectation from a SELECT
passes while the code that answers the caller is broken.

RULE 4 -- EVERY ABSENCE HAS A POSITIVE TWIN. Each refusal class below is
satisfied completely by a portal that refuses everything, and each lockout and
expiry row is satisfied completely by an implementation that locked or expired
the whole organisation. The twins are named `test_the_positive_twin_*`, they
assert on CONTENT (the right patient's own id and name, not a bare 200), and
each one's docstring names the wrong version it exists to catch. NEITHER HALF
OF A PAIR MAY BE DELETED WITHOUT THE OTHER.

WHAT THIS FILE DELIBERATELY DID NOT TEST AT a931869, because none of it existed
there and inventing it is what the queue forbids: a patient report route
(`reports` has no content column), a patient status field (`orders.state` has
seven values and three have no writer anywhere), patient trends (held on
w127-trends), and any administrative route.

TWO OF THOSE FOUR HAVE SINCE BEEN ANSWERED, AND THE PARAGRAPH IS CORRECTED
RATHER THAN LEFT STANDING (Rule 3: fix the claim, not the code) -- because this
is a file an implementer reads to learn what the portal surface is, and a stale
"there is no report route" is how one gets written twice.

  * w134 answered the STATUS question without widening the surface: the
    `stages` list is a DERIVED field on an existing response body, and
    `orders.state` is still returned nowhere.
  * w135 answered the REPORT question by widening it, to
    `GET /portal/me/reports` and `GET /portal/me/reports/{report_id}`. THE
    PARENTHESIS ABOVE IS STILL TRUE AND IS THE REASON THOSE ROUTES ARE SAFE:
    `reports` still has no content column, so what they carry is metadata and
    release state. Both are in AUTHENTICATED_PORTAL_ROUTES below and so are in
    this file's set of size one; their own behavioural rows are in
    test_w135_patient_report_access.py.

Patient trends and administrative routes remain absent, and both are still
asserted to be absent below.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os
import pathlib
import uuid

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
PASSWORD = "correct horse battery staple"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"
CLIENT_IP = "203.0.113.11"

# The one refusal, in BOTH channels. Built the same way w137 builds it, from
# the constant rather than from a string literal copied out of a response.
THE_REFUSAL = (401, json.dumps({"detail": "Authentication failed."}, separators=(",", ":")).encode())

# The FIVE authenticated portal routes and how to call each. The logout route
# is here ON PURPOSE: it is the one whose success is a 204 rather than a 200,
# which is exactly the sort of route a refusal set quietly omits.
#
# w135 added the last two. `{report_id}` is filled with a fixed, real-shaped
# UUID that names nothing: every case in this file is a REFUSED credential, so
# the handler never runs and the id is never looked up. Its only job is to make
# the path routable.
A_REPORT_ID_THAT_NAMES_NOTHING = "1d1c51e4-9e31-4a0d-8a2f-0b6f4cbe7a51"

AUTHENTICATED_PORTAL_ROUTES = (
    ("GET", "/portal/me"),
    ("GET", "/portal/me/tests"),
    ("GET", "/portal/me/reports"),
    ("GET", f"/portal/me/reports/{A_REPORT_ID_THAT_NAMES_NOTHING}"),
    # w136. The report series. It takes no identifier, so there is nothing to
    # fill in — which is also why it has no identifier channel of its own and
    # its only oracle would be the SUCCESS answer (see test_w136).
    ("GET", "/portal/me/trends"),
    ("POST", "/portal/logout"),
)


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
    """Ten attempts per source per minute is the shipped limit. This file
    deliberately makes more than ten login attempts from one source in a single
    test (locking an account out costs five by itself), and a 429 is not the
    refusal any row here is about -- the limiter has its own file."""
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
    ONE organisation with an Administrator, a SECOND organisation that is
    DISABLED, and three enrolled patients in the first.

    Three patients, not one, because every row in this file that refuses
    somebody is satisfied by an implementation that refuses everybody: `alice`
    is the subject, `bob` is the bystander whose session and credential must
    keep working, and `carol` is the one who gets locked out so that Alice's
    lockout row has a bystander of its own.

    The disabled organisation is here because `organisation_disabled` is one of
    `patient_login`'s six failure reasons and it cannot be produced any other
    way.
    """
    org_id = dao.create_organisation("Org A")
    staff_id = dao.create_user(org_id, "admin@a.example", PASSWORD)
    with conn.cursor() as cur:
        # w162. ORDERER ALONGSIDE ADMINISTRATOR: this account registers the
        # three patients below, and registration is now the ordering
        # clinician's (human ruling 2026-09-19). Roles are additive; this
        # file's subject is the PATIENT surface, which consults no staff role.
        for role in ("Administrator", "Orderer"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (staff_id, org_id, role, staff_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
            )
    conn.commit()
    staff = dao.login("admin@a.example", org_id, PASSWORD)

    people = {}
    for tag, name, sex in (("alice", "Alice A", "F"), ("bob", "Bob B", "M"), ("carol", "Carol C", "F")):
        patient_id = dao.create_patient(staff, name, dt.date(1990, 1, 1), sex)
        email = f"{tag}@portal.example"
        login_id = dao.enrol_patient_login(staff, patient_id, email, PASSWORD)
        people[tag] = {"patient_id": patient_id, "login_id": login_id, "email": email, "name": name}

    disabled_org_id = dao.create_organisation("Disabled Org")
    with conn.cursor() as cur:
        cur.execute("UPDATE organisations SET disabled = TRUE WHERE org_id = %s", (disabled_org_id,))
    conn.commit()

    return {
        "org_id": org_id,
        "disabled_org_id": disabled_org_id,
        "staff_id": staff_id,
        "staff": staff,
        **people,
    }


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678))


# ─── helpers ─────────────────────────────────────────────────────────────────


def _reset_throttle():
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()


def _post_portal_login(client, org_id, email, password=PASSWORD):
    """One attempt at the REAL route, throttle cleared first. Returns the
    response; the caller decides whether it expected a 204."""
    _reset_throttle()
    return client.post("/portal/login", json={"email": email, "org_id": str(org_id), "password": password})


def _portal_session(client, org_id, email, password=PASSWORD):
    """Authenticate over HTTP and return the opaque id the browser was given.

    Through the route, not by calling `DataAccess.patient_login` in-process: a
    cookie minted in-process is not the credential a browser holds, and this
    file's whole subject is what happens at the route.
    """
    response = _post_portal_login(client, org_id, email, password)
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _staff_session(client, org_id, email, password=PASSWORD):
    _reset_throttle()
    response = client.post("/auth/login", json={"email": email, "org_id": str(org_id), "password": password})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _call(client, method, path, cookies):
    return client.get(path, cookies=cookies) if method == "GET" else client.post(path, cookies=cookies)


def _lock_the_credential_out(client, org_id, email):
    """
    Drive a real lockout through the real route: MAX_FAILED_LOGIN_ATTEMPTS
    wrong passwords, which is what sets `locked_until`.

    NOT an UPDATE against `patient_logins`. The lockout is a behaviour of
    `patient_login`, and a fixture that wrote the column directly would pass
    against a version that had stopped counting.
    """
    from clinical.data_access import MAX_FAILED_LOGIN_ATTEMPTS

    for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
        assert _post_portal_login(client, org_id, email, "wrong password").status_code == 401
    return MAX_FAILED_LOGIN_ATTEMPTS


def _age_the_session(conn, session_id, *, past_absolute_cap=False, past_idle_window=False):
    """
    Move ONE session's windows into the past.

    THIS IS A FIXTURE, NOT AN EXPECTATION. The route under test builds its own
    `DataAccess` per request with a `SystemClock` (`clinical/endpoints.py::
    _get_data_access`), so a fake clock cannot be injected from out here and
    the only honest way to reach "an hour later" is to move the row. Rule 2a
    forbids building the EXPECTATION from a raw query; it does not forbid
    reaching a state with one. Nothing below reads this row back -- every
    assertion is taken from the route's own answer.

    Scoped to one session id on purpose: a helper that aged the whole table
    would silently satisfy the bystander twins.
    """
    session_id = uuid.UUID(str(session_id))
    with conn.cursor() as cur:
        if past_absolute_cap:
            # The absolute cap is checked BEFORE the idle window, so this row
            # tests the cap and nothing else.
            cur.execute(
                "UPDATE patient_sessions SET expires_at = %s WHERE session_id = %s",
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1), session_id),
            )
        if past_idle_window:
            # expires_at is left in the future deliberately: if this row moved
            # both, it would pass against a portal that had stopped checking
            # the idle window entirely.
            cur.execute(
                "UPDATE patient_sessions SET last_activity_at = %s - (idle_timeout_minutes + 1) "
                "* INTERVAL '1 minute' WHERE session_id = %s",
                (dt.datetime.now(dt.timezone.utc), session_id),
            )
        assert cur.rowcount == 1, f"the fixture aged {cur.rowcount} rows, not exactly one"
    conn.commit()


def _patient_login_failure_reasons():
    """
    The failure reasons `patient_login` actually has, READ OUT OF ITS OWN
    SOURCE.

    Derived rather than typed, because a maintained literal is precisely how
    `test_w132_login_refusal_parity.py::_login_probes` came to claim "all six
    of patient_login()'s" while exercising four. A seventh reason added to the
    method later fails the census row below instead of being missed.

    BY AST OVER THE FILE, NOT `inspect.getsource(DataAccess.patient_login)`.
    That was tried first and it is a trap worth recording: `patient_login`
    carries `@auditable` and `@transactional`, so the attribute on the class
    is a decorator wrapper and `getsource` returns `def wrapper(self, *args,
    **kwargs)` -- 10,860 characters of the DECORATOR, containing none of the
    six reasons. The regex matched nothing and the census would have been
    `set()`. It failed loudly only because the row asserts `len(...) == 6`
    BEFORE comparing; an equality against the probe map alone would have
    compared two empty-ish things and passed vacuously. That is Rule 4 in
    miniature, and the reason the length assertion is there.
    """
    module = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "patient_login"
    ]
    assert len(functions) == 1, f"expected exactly one def patient_login in data_access.py, found {len(functions)}"
    return {
        keyword.value.value
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant)
    }


# ─── 1. all six patient-login failure reasons, in one set ────────────────────


class TestTheLoginRefusalIsOneAnswerOverAllSixReasons:
    """
    "No information oracle between a NONEXISTENT and an UNAUTHORISED patient"
    -- and on this surface `POST /portal/login` is the ONLY channel in which
    the two can be told apart, because COUNTED at a931869 no route on this app
    accepts a patient identifier in any channel.

    The two reasons w132's set omits are the two UNAUTHORISED ones: a
    credential that exists and is locked, and a credential that exists and has
    been revoked. Against an address that exists nowhere, those are the whole
    question.
    """

    def _probes(self, client, dao, world):
        """One request per failure reason, all six, through the real route."""
        org = world["org_id"]

        # locked_out -- Carol, so Alice stays available to the other rows.
        _lock_the_credential_out(client, org, world["carol"]["email"])

        # patient_login_disabled -- through the real Administrator-gated
        # method, not an UPDATE, for the reason _lock_the_credential_out gives.
        dao.set_patient_login_disabled(world["staff"], world["bob"]["login_id"], True)

        return {
            "unknown_organisation": _post_portal_login(client, uuid.uuid4(), world["alice"]["email"]),
            "organisation_disabled": _post_portal_login(client, world["disabled_org_id"], world["alice"]["email"]),
            "unknown_patient_login": _post_portal_login(client, org, "nobody@portal.example"),
            "invalid_password": _post_portal_login(client, org, world["alice"]["email"], "wrong password"),
            # THE TWO THAT WERE IN NO SET.
            "locked_out": _post_portal_login(client, org, world["carol"]["email"]),
            "patient_login_disabled": _post_portal_login(client, org, world["bob"]["email"]),
        }

    def test_the_six_reasons_are_read_from_the_method_and_all_six_are_probed(self, client, dao, world):
        """
        THE NON-VACUITY ROW. Without it, `_probes` is a literal somebody can
        shorten, which is the exact defect this class was written for.

        Catches: a seventh failure reason added to `patient_login` and probed
        by nobody. Vacuous against: any oracle at all -- it asserts nothing
        about what the route answers, which is why it is paired with the row
        below and neither stands alone.
        """
        declared = _patient_login_failure_reasons()
        assert len(declared) == 6, f"patient_login's failure reasons changed: {sorted(declared)}"
        probed = set(self._probes(client, dao, world))
        assert probed == declared, f"probed {sorted(probed)} but the method has {sorted(declared)}"

    def test_every_one_of_the_six_is_byte_identical_in_both_channels(self, client, dao, world):
        """
        RULE 8 as a SET OF SIZE ONE. Not six equalities -- a set, so a leak in
        a reason nobody thought about still fails.

        Catches: any version that answers a locked or revoked credential
        differently from an unknown address -- a 423, a "this account is
        locked", a 403, a different detail string. Vacuous against: a portal
        that refuses every login ever made, which is what the twin below
        catches.
        """
        observed = {
            reason: (response.status_code, response.content)
            for reason, response in self._probes(client, dao, world).items()
        }
        assert len(observed) == 6, observed
        assert len(set(observed.values())) == 1, observed
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_no_refusal_body_names_the_address_the_organisation_or_the_patient(self, client, dao, world):
        """
        The body channel specifically, and the identifiers specifically. A
        refusal that echoed the address back would confirm which of the six
        states it was in for anybody who could compare two of them.
        """
        for reason, response in self._probes(client, dao, world).items():
            text = response.text
            for leak in (
                world["alice"]["email"],
                world["carol"]["email"],
                world["bob"]["email"],
                str(world["org_id"]),
                str(world["alice"]["patient_id"]),
                "locked",
                "disabled",
            ):
                assert leak not in text, f"{reason} leaks {leak!r}: {text}"

    def test_the_positive_twin_a_good_credential_is_not_in_that_set(self, client, dao, world):
        """
        THE ROW THAT DOES THE OPPOSITE JOB. Every row above is satisfied
        completely by a `/portal/login` that answers 401 to everybody.

        Catches: a portal login route that authenticates nobody. Vacuous
        against: every oracle the rows above catch.

        Asserted on CONTENT, not on "did not raise": the session it mints must
        reach ALICE's record, because a 204 that hands out somebody else's
        session is the failure that matters most.
        """
        _lock_the_credential_out(client, world["org_id"], world["carol"]["email"])
        dao.set_patient_login_disabled(world["staff"], world["bob"]["login_id"], True)

        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json()["patient_id"] == str(world["alice"]["patient_id"])
        assert response.json()["name"] == "Alice A"

    def test_the_positive_twin_locking_one_credential_locks_only_that_one(self, client, world):
        """
        THE OTHER OPPOSITE. `locked_out` being indistinguishable is satisfied
        completely by a lockout that locked the whole organisation, or by one
        that locked nobody and answered `invalid_password` instead.

        Catches BOTH: that Carol is genuinely locked is proved by her CORRECT
        password being refused (a version that never locked her would mint a
        session), and that the lockout is scoped is proved by Alice's correct
        password still working in the same test.
        """
        _lock_the_credential_out(client, world["org_id"], world["carol"]["email"])

        carol = _post_portal_login(client, world["org_id"], world["carol"]["email"])
        assert (carol.status_code, carol.content) == THE_REFUSAL, carol.text

        alice = _post_portal_login(client, world["org_id"], world["alice"]["email"])
        assert alice.status_code == 204, alice.text

    def test_the_positive_twin_revoking_one_credential_revokes_only_that_one(self, client, dao, world):
        """
        The same shape for `patient_login_disabled`. Catches a revocation that
        disabled every credential in the organisation -- which satisfies the
        set-of-size-one row perfectly.
        """
        dao.set_patient_login_disabled(world["staff"], world["bob"]["login_id"], True)

        bob = _post_portal_login(client, world["org_id"], world["bob"]["email"])
        assert (bob.status_code, bob.content) == THE_REFUSAL, bob.text

        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json()["patient_id"] == str(world["alice"]["patient_id"])


# ─── 2. an expired patient session, both kinds, at every route ───────────────


class TestAnExpiredPatientSessionIsRefusedAtEveryPortalRoute:
    """
    Wave 4 names "expired/terminated session". COUNTED at a931869: terminated
    is covered twice (logout in test_w132_logout.py, revocation in
    test_w137_patient_backend.py) and EXPIRED is covered at no route, of
    either kind, and absolute expiry of a patient session is asserted at no
    layer at all.

    `patient_session_valid` refuses on three conditions -- terminated_at,
    now >= expires_at, now >= last_activity_at + idle_timeout_minutes -- and
    all three raise `SessionExpired`, which `_get_patient_session` converts by
    catching `AuthenticationError`.
    """

    def test_session_expired_is_an_authentication_error(self):
        """
        THE ONE-WORD HAZARD, pinned structurally as well as behaviourally.

        Catches: `class SessionExpired(Exception)` -- one edit, after which
        `_get_patient_session`'s `except AuthenticationError` stops catching
        it, an expired session answers 500 and an unknown one answers 401, and
        the status channel tells a caller that an id names a real, once-valid
        session. Rule 8: an oracle can live in EITHER channel.

        Vacuous against: every leak in the body channel. It is the cheap
        structural half of the behavioural rows below and does not replace
        them.
        """
        from clinical.data_access import AuthenticationError, SessionExpired

        assert issubclass(SessionExpired, AuthenticationError)

    def test_a_session_past_its_absolute_cap_is_refused_at_every_portal_route(self, client, conn, world):
        """
        Catches: a portal that stopped checking `expires_at` -- a 24-hour
        credential that never ends. Vacuous against: a portal that refuses
        everything (see the twin), and against the idle window specifically,
        which is why the fixture moves ONLY `expires_at`.
        """
        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: cookie}).status_code == 200

        _age_the_session(conn, cookie, past_absolute_cap=True)

        for method, path in AUTHENTICATED_PORTAL_ROUTES:
            response = _call(client, method, path, {PATIENT_COOKIE: cookie})
            assert (response.status_code, response.content) == THE_REFUSAL, (path, response.text)

    def test_a_session_past_its_idle_window_is_refused_at_every_portal_route(self, client, conn, world):
        """
        The other kind, and `expires_at` is deliberately left in the future so
        this row can only pass if the IDLE check is the one doing the work.

        Catches: a portal that dropped the idle window while keeping the
        absolute cap -- an abandoned session on a shared machine staying live
        for the rest of the day.
        """
        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: cookie}).status_code == 200

        _age_the_session(conn, cookie, past_idle_window=True)

        for method, path in AUTHENTICATED_PORTAL_ROUTES:
            response = _call(client, method, path, {PATIENT_COOKIE: cookie})
            assert (response.status_code, response.content) == THE_REFUSAL, (path, response.text)

    def test_the_positive_twin_a_session_inside_both_windows_still_answers(self, client, world):
        """
        Catches: a portal that refuses every session, which satisfies both
        rows above completely. Asserted on CONTENT -- Alice's own id and name,
        and explicitly not Bob's -- because a 200 carrying the wrong patient is
        worse than a refusal.
        """
        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["patient_id"] == str(world["alice"]["patient_id"]), body
        assert body["name"] == "Alice A", body
        assert body["patient_id"] != str(world["bob"]["patient_id"]), body

    def test_the_positive_twin_expiring_one_session_expires_only_that_one(self, client, conn, world):
        """
        THE ROW THE ABSENCE ASSERTIONS CANNOT DO WITHOUT. "The aged session is
        refused" is satisfied completely by an implementation that expired
        every patient session in the organisation the moment one aged.

        Catches exactly that. Bob's session is minted at the same time, aged by
        nothing, and must still return BOB -- his own id, not merely a 200 and
        not Alice's record.
        """
        alice_cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        bob_cookie = _portal_session(client, world["org_id"], world["bob"]["email"])
        assert alice_cookie != bob_cookie

        _age_the_session(conn, alice_cookie, past_absolute_cap=True)

        refused = client.get("/portal/me", cookies={PATIENT_COOKIE: alice_cookie})
        assert (refused.status_code, refused.content) == THE_REFUSAL, refused.text

        response = client.get("/portal/me", cookies={PATIENT_COOKIE: bob_cookie})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["patient_id"] == str(world["bob"]["patient_id"]), body
        assert body["name"] == "Bob B", body

    def test_an_expired_session_cannot_be_revived_by_using_it(self, client, conn, world):
        """
        `patient_session_valid` REFRESHES `last_activity_at` on every
        successful validation. A version that refreshed BEFORE checking, or
        that refreshed on the failure path, would hand an idle-expired session
        a fresh window on its first rejected request -- so the second request
        would succeed and no single-request test would see it.

        Catches exactly that. Vacuous against every body-channel leak.
        """
        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])
        _age_the_session(conn, cookie, past_idle_window=True)

        for attempt in range(3):
            response = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
            assert (response.status_code, response.content) == THE_REFUSAL, (attempt, response.text)


# ─── 3. the set of size one, over the whole patient surface ──────────────────


class TestEveryPortalRefusalIsOnePairOnEveryRoute:
    """
    RULE 8, WIDENED FROM ONE ROUTE TO THE SURFACE.

    COUNTED at a931869: both existing set-of-size-one rows collect over
    `GET /portal/me` alone. That is a real hole, because the three
    authenticated handlers do not catch identically in source --
    `get_my_record` catches (NotFoundError, AuthorizationError) and the other
    two catch AuthorizationError only -- so their refusal behaviour is three
    separate pieces of code, and only one of them is in a set.

    Eight cases x three routes = 24 observations, ONE pair.
    """

    def _cases(self, client, dao, conn, world):
        """
        Every way a caller can present something that is not a live patient
        session. Two of them -- the two expiries -- are new to this wave; the
        rest are w137's six, re-collected here across all three routes.
        """
        logged_out = _portal_session(client, world["org_id"], world["bob"]["email"])
        assert client.post("/portal/logout", cookies={PATIENT_COOKIE: logged_out}).status_code == 204

        revoked = _portal_session(client, world["org_id"], world["carol"]["email"])
        dao.set_patient_login_disabled(world["staff"], world["carol"]["login_id"], True)

        absolute = _portal_session(client, world["org_id"], world["alice"]["email"])
        _age_the_session(conn, absolute, past_absolute_cap=True)

        idle = _portal_session(client, world["org_id"], world["alice"]["email"])
        _age_the_session(conn, idle, past_idle_window=True)

        staff_cookie = _staff_session(client, world["org_id"], "admin@a.example")

        return {
            "no credential at all": {},
            "a malformed id": {PATIENT_COOKIE: "not-a-uuid"},
            "a well-formed id that names nothing": {PATIENT_COOKIE: str(uuid.uuid4())},
            "a genuine STAFF session id": {PATIENT_COOKIE: staff_cookie},
            "a session ended by logout": {PATIENT_COOKIE: logged_out},
            "a session ended by revocation": {PATIENT_COOKIE: revoked},
            "a session past its absolute cap": {PATIENT_COOKIE: absolute},
            "a session past its idle window": {PATIENT_COOKIE: idle},
        }

    def test_eight_cases_on_five_routes_are_one_pair(self, client, dao, conn, world):
        """
        Catches: an oracle introduced on `/portal/me/tests` or
        `/portal/logout` -- the two routes in NO set at a931869. A
        `/portal/me/tests` that answered `200 []` to an unknown session, or a
        `/portal/logout` that answered 204 to one, fails here and fails
        nowhere else in this repository.

        Vacuous against: a portal that refuses everything, and against a portal
        that returns the WRONG patient's data with a 200 -- both are the twins'
        job.
        """
        cases = self._cases(client, dao, conn, world)
        assert len(cases) == 8, "the case list shrank -- this row would prove less than it claims"
        assert len(AUTHENTICATED_PORTAL_ROUTES) == 6, "a portal route left the list without being probed"

        observed = {}
        for name, cookies in cases.items():
            for method, path in AUTHENTICATED_PORTAL_ROUTES:
                response = _call(client, method, path, cookies)
                observed[f"{name} | {method} {path}"] = (response.status_code, response.content)

        # 8 x 6. Was 8 x 3 at 863f07e; w135 added the two report routes and
        # w136 the series route, so the same eight credential states now
        # collapse forty-eight observations into one pair instead of
        # twenty-four.
        assert len(observed) == 48, observed
        assert len(set(observed.values())) == 1, {k: (s, b[:120]) for k, (s, b) in observed.items()}
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_no_refusal_on_any_route_names_any_identifier(self, client, dao, conn, world):
        """
        The body channel on its own, across the whole surface. A set of size
        one is already violated by a body that differed -- this row exists so
        that a FUTURE change which leaks the SAME identifier into ALL of them
        (and so keeps the set at size one) still fails.
        """
        cases = self._cases(client, dao, conn, world)
        forbidden = [str(world[tag]["patient_id"]) for tag in ("alice", "bob", "carol")]
        forbidden += [world[tag]["email"] for tag in ("alice", "bob", "carol")]
        forbidden += [str(world["org_id"])]

        for name, cookies in cases.items():
            for method, path in AUTHENTICATED_PORTAL_ROUTES:
                text = _call(client, method, path, cookies).text
                for leak in forbidden:
                    assert leak not in text, f"{name} | {method} {path} leaks {leak!r}: {text}"

    def test_the_positive_twin_a_live_session_is_not_in_that_set_on_any_route(self, client, world):
        """
        Without this, "everything is the one refusal" is satisfied perfectly by
        a portal that refuses everything, on every route at once.

        Catches exactly that, and asserts each route's OWN success shape --
        /portal/me returns Alice's record, /portal/me/tests and
        /portal/me/reports return lists, /portal/logout returns 204 -- because
        "not the refusal pair" alone would be satisfied by four different
        500s.

        NEITHER THIS ROW NOR THE SET ROW MAY BE DELETED WITHOUT THE OTHER.
        """
        cookie = _portal_session(client, world["org_id"], world["alice"]["email"])

        me = client.get("/portal/me", cookies={PATIENT_COOKIE: cookie})
        assert (me.status_code, me.content) != THE_REFUSAL
        assert me.status_code == 200, me.text
        assert me.json()["patient_id"] == str(world["alice"]["patient_id"])

        tests = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
        assert (tests.status_code, tests.content) != THE_REFUSAL
        assert tests.status_code == 200, tests.text
        assert tests.json() == []

        reports = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: cookie})
        assert (reports.status_code, reports.content) != THE_REFUSAL
        assert reports.status_code == 200, reports.text
        assert reports.json() == []

        # `GET /portal/me/reports/{report_id}` IS DELIBERATELY NOT TWINNED
        # HERE, and the omission is the point rather than an oversight: for a
        # live session asking after a report id that names nothing, THE
        # REFUSAL IS THE CORRECT ANSWER -- that is exactly how w135 keeps a
        # nonexistent report indistinguishable from another patient's. Its
        # positive twin needs a really-released report, so it lives in
        # test_w135_patient_report_access.py, where one can be built.
        logout = client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})
        assert (logout.status_code, logout.content) != THE_REFUSAL
        assert logout.status_code == 204, logout.text


# ─── 4. the surface itself: what is served, and what accepts an identifier ───


class TestThePatientSurfaceIsSixRoutesAndAcceptsNoPatientIdentifier:
    """
    The census, as a test rather than as a sentence in a document.

    STANDING RULE 9, IN ITS CORRECTED FORM. `app.routes` is not the answer:
    `include_router` leaves opaque `_IncludedRouter` wrappers, and naive
    iteration on this app reports a surface of TWO. This walks the tree,
    descending into wrappers, with the non-vacuity checks the correction
    requires -- refusing both the empty answer and the wrapper-shaped one.
    """

    @staticmethod
    def _walk(routes, prefix=""):
        leaves = []
        for route in routes:
            original = getattr(route, "original_router", None)
            children = getattr(route, "routes", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                leaves += TestThePatientSurfaceIsSixRoutesAndAcceptsNoPatientIdentifier._walk(
                    original.routes, prefix + (getattr(context, "prefix", "") if context else "")
                )
            elif children:
                leaves += TestThePatientSurfaceIsSixRoutesAndAcceptsNoPatientIdentifier._walk(
                    children, prefix + getattr(route, "path", "")
                )
            else:
                leaves.append((prefix + getattr(route, "path", ""), route))
        return leaves

    def _leaves(self, clinical_dsn):
        from clinical.app import app

        top = list(app.routes)
        leaves = self._walk(top)
        assert leaves, "the walk found nothing -- this whole class would pass vacuously"
        assert all(path for path, _ in leaves), "a leaf has an empty path -- the walk stopped at a wrapper"
        assert len(leaves) > len(top), (
            f"the walk returned the wrapper-shaped answer ({len(leaves)} leaves vs {len(top)} top-level "
            "entries) -- Rule 9's counter-example"
        )
        return leaves

    def test_no_served_route_takes_a_patient_identifier_in_the_path(self, clinical_dsn):
        """
        Derived from what the app SERVES, not from the portal router alone --
        the staff routers are in scope too, because a
        `GET /patients/{patient_id}/...` added over there is just as much a
        patient identifier in a URL.

        Catches: the held `GET /patients/{patient_id}/variant-trends` being
        mounted as-is (w127-trends), which is the exact shape Wave 8 was warned
        about. Vacuous against: an identifier arriving in a query string or a
        body -- covered by test_portal_endpoints_http.py's three-channel rows.

        THE EXEMPTION, w152, AND WHY IT IS NOT A WEAKENING. The property this
        guard stands for is the one this file's own class docstring states:
        "the principal there is STAFF, so it is not a breach of the
        patient-identity ruling until a patient session reaches it. These
        rows assert that no patient session can." Until w152 there was no
        staff route naming anything patient-shaped, so the blanket form of
        the ban was free and was taken.

        `POST /admin/patient-logins/{patient_login_id}/disabled` is the first
        one. It names a CREDENTIAL, not a patient -- w133 made that
        distinction load-bearing in `set_patient_login_disabled`'s own
        docstring -- and it is on the STAFF router with the STAFF dependency,
        so a patient session presented to it is refused as the anonymous
        refusal.

        SO THE EXEMPTION CARRIES ITS OWN PROOF, three ways, rather than being
        a hole: the exempt path must actually be SERVED (a stale exemption
        fails here rather than quietly widening the filter), it must carry
        `clinical.endpoints._get_session`, and the behavioural half -- that a
        real live patient session id is refused on it -- is
        `test_w152_admin_surface.py::TestTheUnauthenticatedCallIsRefused::
        test_a_patient_session_id_is_not_a_staff_credential`.
        """
        leaves = self._leaves(clinical_dsn)
        served = {path for path, _ in leaves}

        exempt = {
            "/admin/patient-logins/{patient_login_id}/disabled",
            # w155 (WAVE 5). The second exemption. It names a PATIENT, not a
            # credential, and it is exempt on exactly the property this
            # class's docstring states and on nothing else: the principal
            # there is STAFF and no patient session can reach it. The `for`
            # loop below PROVES that for every exempt path rather than
            # trusting this comment -- each must carry
            # `clinical.endpoints._get_session`. The behavioural half is
            # `test_w155_patient_administration.py::TestAdministratorOnlyEnrolment::
            # test_a_live_patient_session_is_not_a_staff_credential`, which
            # presents a REAL live portal session id to this exact route and
            # gets the anonymous refusal.
            "/admin/patients/{patient_id}/logins",
            # w162. THE THIRD, and it is exempt on the SAME property and on
            # nothing else: `POST /clinic/patients/{patient_id}/consents` is a
            # STAFF route behind `clinical.endpoints._get_session`, which the
            # `for` loop below PROVES for it rather than taking on trust. The
            # identifier's organisation comes from `Session.org_id` and never
            # from the request, so it selects a row inside the caller's own
            # tenant and can select nothing else. The behavioural halves are
            # `test_w162_registration_and_consent.py::
            # TestOnlyTheClinicianMayRecordConsent` and
            # `::TestTheOtherOrganisationIsNotAddressable`, which plants a real
            # patient in the other organisation and names it.
            "/clinic/patients/{patient_id}/consents",
            # Genome Analysis, Phase 1. THE FOURTH AND FIFTH, on the SAME property
            # and nothing else: two READ-ONLY STAFF routes behind
            # `clinical.endpoints._get_session` (the loop below PROVES it).
            # `list_patient_genome_analyses` gates on role first, binds
            # `p.patient_id = %s AND p.org_id = %s` with `org_id` from
            # `Session.org_id`, and answers a patient in another organisation
            # byte-identically to one that does not exist. Behavioural halves:
            # `test_genome_analysis_endpoints.py::TestClinicianRoutes`
            # (cross-organisation, role-gate-before-existence).
            "/clinic/patients/{patient_id}/genome/chromosomes",
            "/clinic/patients/{patient_id}/genome/chromosomes/{name}",
            # Genome Analysis, Phase 2: the same property again for the two
            # read-only Explorer map routes (staff session, role gate first,
            # organisation from Session.org_id, audited as genome_read);
            # behavioural halves in `test_genome_map_endpoints.py`.
            "/clinic/patients/{patient_id}/genome/map",
            "/clinic/patients/{patient_id}/genome/map/{name}",
            # Milestone 1. THE SIXTH, on the SAME property and nothing else: `GET /clinic/patients/{patient_id}` is a
            # READ-ONLY STAFF route behind `clinical.endpoints._get_session` (the loop below PROVES it). `read_patient`
            # gates on role first (Orderer, Interpreter, Approver), binds `org_id` from `Session.org_id` and never from
            # the request, is audited, and answers a patient in another organisation byte-identically to one that does
            # not exist. (`GET /clinic/patients/{patient_id}/consents` shares the path already exempted above.)
            # Behavioural halves: `test_staff_read_endpoints.py::TestOrganisationIsolation` and `::TestTheRoleMatrix`.
            "/clinic/patients/{patient_id}",
        }
        assert exempt <= served, (
            f"the exemption names {sorted(exempt - served)}, which this app does not serve -- "
            "a stale exemption silently widens the filter below"
        )

        from clinical.endpoints import _get_session

        for path, route in leaves:
            if path in exempt:
                dependencies = [d.call for d in getattr(route, "dependant").dependencies]
                assert _get_session in dependencies, (
                    f"{path} is exempted from the patient-identifier ban because it is a STAFF route, "
                    f"and it does not carry the staff session dependency: {dependencies}"
                )

        offenders = [path for path, _ in leaves if "{patient" in path and path not in exempt]
        assert not offenders, f"served route(s) naming a patient in the path: {offenders}"

    def test_the_patient_surface_is_exactly_these_seven_routes(self, clinical_dsn):
        """
        NOT a hard-coded total used as proof of future safety (the queue
        forbids that) -- an EXPLICIT SET, so the failure message names what
        arrived or left. An EIGHTH patient route is a thing somebody must look
        at: the report routes arrived in w135 and were counted here, the
        status question was answered in w134 by widening a BODY rather than
        the surface, and w136 added the report SERIES.

        RULE 3 APPLIED TO THIS DOCSTRING (fix the claim, not the code). It
        used to end "trends remains an open product ruling held on
        w127-trends". Half of that is now false and half is still true, so
        neither half is left to be read as the other: w136 added
        `GET /portal/me/trends`, a series whose points are the patient's own
        RELEASED REPORTS; what remains an open product ruling, and is still
        held on w127-trends and still absent from this set, is a series of
        VARIANT CLASSIFICATIONS read out of raw `run_document`.
        """
        served = {path for path, _ in self._leaves(clinical_dsn) if path.startswith("/portal")}
        assert served == {
            "/portal/login",
            "/portal/logout",
            "/portal/me",
            "/portal/me/tests",
            # w135. Metadata and release state only -- `reports` has no content
            # column, so there is no document here to expose. The report id in
            # the second is a RESOURCE identifier; the patient is still bound
            # from the session and `{patient` still appears in no served path,
            # which the row above asserts.
            "/portal/me/reports",
            "/portal/me/reports/{report_id}",
            # w136. The report SERIES. No identifier in any channel, and no
            # classification in the body -- it is deliberately not the held
            # `/patients/{patient_id}/variant-trends`, which the row above
            # would catch if anybody mounted it.
            "/portal/me/trends",
        }, served

    def test_no_administrative_operation_is_reachable_from_a_NON_administrative_module(self):
        """
        RULE 3 APPLIED TO THIS DOCSTRING — fix the claim, not the code.

        It used to read "the brief puts the first administrative HTTP route
        out of scope for this wave" and was named
        `test_there_are_still_no_administrative_routes`. That sentence was
        true of w138's brief and is FALSE of w152's, which is the wave that
        builds the administrative surface: `clinical/admin_endpoints.py` now
        serves four administrative routes and the human's own dispatch asks
        for them. Leaving the old name in place would have made this row read
        as a standing prohibition that the tree contradicts.

        WHAT THE ROW ACTUALLY PROVED, AND STILL PROVES, IS NARROWER AND IS
        NOT WEAKENED: the three NON-administrative route modules must not
        grow an administrative call. That was always the mechanism — the scan
        never opened any other file — and it is the property that matters
        now that an administrative module exists: an Administrator-gated
        operation invoked from the patient portal's module, or from the login
        module, would inherit that module's dependency rather than the staff
        one, and no route census would notice.

        AND IT IS NO LONGER AN ABSENCE OVER AN EMPTY SUBJECT. The positive
        twin below requires that `clinical/admin_endpoints.py` DOES call
        them; without it, a rename of every method in `DataAccess` would
        leave this row green while proving nothing at all.
        """
        import clinical.admin_endpoints
        import clinical.auth_endpoints
        import clinical.endpoints
        import clinical.portal_endpoints

        administrative = (
            "enrol_patient_login",
            "set_patient_login_disabled",
            "set_user_disabled",
            "assign_role",
            "terminate_session",
            "enrol_totp",
        )

        def _body(module):
            source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
            return source.split('"""', 2)[-1] if source.lstrip().startswith('"""') else source

        for module in (clinical.endpoints, clinical.auth_endpoints, clinical.portal_endpoints):
            body = _body(module)
            for name in administrative:
                assert f"dao.{name}" not in body, (
                    f"{module.__name__} now calls {name} -- an administrative operation belongs on the "
                    "administrative router, with the staff dependency, or it is not administered at all"
                )

        # THE POSITIVE TWIN. w152 wired three of the six; w155 wired patient
        # credential ISSUANCE, which w152 had stopped on spec 16 and which the
        # human's Wave 5 dispatch then instructed in terms (see
        # hive/agents/god/w155-decision-cards.md, Card 3 -- the tension with
        # 16 is recorded, not dissolved). w164 wired TOTP ENROLMENT: the two
        # things it was stopped on -- who supplies the secret, and where the
        # cipher key lives -- were both answered FROM THE REPOSITORY (the
        # method generates the secret; the key comes from the environment,
        # exactly as `clinical/bootstrap.py` already rules for the database
        # passwords), so the stop was lifted by evidence rather than by a
        # guess. `terminate_session` still has no route. At least one must be
        # present or the scan above is an absence over a vocabulary that no
        # longer exists.
        admin_body = _body(clinical.admin_endpoints)
        wired = [name for name in administrative if f"dao.{name}" in admin_body]
        assert wired == [
            "enrol_patient_login",
            "set_patient_login_disabled",
            "set_user_disabled",
            "assign_role",
            "enrol_totp",
        ], wired

    def test_patient_credential_ISSUANCE_is_wired_ONLY_on_the_administrative_router(self):
        """
        RULE 3 APPLIED TO THIS ROW — fix the claim, not the code. RENAMED at
        w155 from `test_patient_credential_ISSUANCE_is_still_not_wired_anywhere`.

        WHAT IT USED TO SAY, AND WHY IT WAS RIGHT. `GEPER_CLINICAL_PLATFORM_SPEC.md`
        16 reads "Recommendation: no patient access in v1" and 33 lists
        "Patient portal access" as a decision required. `enrol_patient_login`
        is the ONLY way a portal credential can come to exist, so w152 held
        that wiring it would settle a product ruling BY IMPLEMENTATION, wired
        the REVOCATION half (which only ever contracts access) and stopped
        the issuance half. This row made the stop durable.

        WHAT SUPERSEDED IT. The human's Wave 5 dispatch instructs "Wire
        patient enrolment and credential operations through the
        administrative surface" and sets eight requirements for it. A ruling
        that was MADE, by the person entitled to make it, is not the same
        object as one that is missing. 16 is still unedited and the tension
        is RECORDED rather than dissolved -- see
        `hive/agents/god/w155-decision-cards.md` Card 3 and
        `clinical/admin_endpoints.py`'s module docstring.

        WHAT THIS ROW PROVES NOW, AND IT IS NOT NOTHING. Issuance is wired in
        EXACTLY ONE PLACE and that place is the administrative router. The
        three NON-administrative route modules must never issue a credential:
        one that did would inherit that module's dependency -- the PATIENT
        one, in `portal_endpoints.py`, or NONE AT ALL in `auth_endpoints.py`
        -- and would be public patient self-registration with no census
        noticing. That is the property the human's requirement "no public
        patient self-registration" actually names, and it is stronger than
        the blanket ban was, because the blanket ban was satisfied by the
        capability being unreachable rather than by it being reachable only
        from the right place.
        """
        import clinical.admin_endpoints
        import clinical.auth_endpoints
        import clinical.endpoints
        import clinical.portal_endpoints

        def _code(module):
            source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
            return source.split('"""', 2)[-1] if source.lstrip().startswith('"""') else source

        for module in (clinical.endpoints, clinical.auth_endpoints, clinical.portal_endpoints):
            assert "dao.enrol_patient_login" not in _code(module), (
                f"{module.__name__} now issues a patient portal credential. Issuance belongs on the "
                "administrative router behind the STAFF dependency, or it is public self-registration"
            )

        # THE POSITIVE TWIN, without which the three absences above are an
        # absence over a name nothing uses: the administrative module DOES
        # issue, so the scan is looking for something that exists.
        assert "dao.enrol_patient_login" in _code(clinical.admin_endpoints)
