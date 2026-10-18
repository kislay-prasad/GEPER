"""
clinical/tests/test_w139_remaining_w132_items.py
────────────────────────────────────────────────
w139 — THE FOUR REMAINING w132 ITEMS, each with a stated verdict and the
evidence that decides it. Wave 5 of the overnight queue, at `863f07e`.

The four are not the same kind of thing, and the file is laid out so that a
reader can tell which is which without reading the code:

  (a) THE LOCKED-OUT BRANCH SPENT NO PASSWORD WORK.  **A REAL DEFECT, FIXED.**
      `login()` and `patient_login()` both returned from the locked-out branch
      without calling `_equalise_password_work`, so that branch was the ONE
      failure path in either method that spent no bcrypt at all. A locked
      address answered measurably faster than a wrong password against a live
      one, and only an address that EXISTS can be locked — so "cheap" confirmed
      the address, and an attacker can manufacture the state themselves with
      five wrong guesses. Two lines, one per method. §1.

  (b) `org_id` AS AN EXPLICIT LOGIN BODY FIELD.  **NOT A DEFECT.** It is load-
      bearing (an address is unique only WITHIN an organisation) and it is not
      an oracle in any of the three channels. §2 proves that rather than
      asserting it.

  (c) `Secure` COOKIE WITH NO TLS.  **NOT A CODE DEFECT — A DEPLOYMENT CARD.**
      The flag makes a plain-HTTP deployment fail CLOSED (the browser never
      sends the credential back), and removing it would be the defect. §3
      demonstrates the consequence through a spec cookie policy rather than
      asserting it from the source comment.

  (d) `POST /auth/logout` WAS IN NO REFUSAL-PARITY SET.  **A REAL COVERAGE GAP,
      CLOSED.** w138 F-3 / C-16: the patient half was closed by
      `test_w138_patient_portal_api.py`, the staff half was left to this wave.
      §4. Test-only; nothing about that route's behaviour changes.

AND ONE THING THAT WAS NOT ON THE LIST, FOUND WHILE ESTABLISHING (b) AND
REPORTED RATHER THAN FIXED — **C-17**. An anonymous caller reads the clinical
database's reachability off either login route by sending a MALFORMED BODY:
422 when the database is up, the generic 401 when it is unreachable or
unconfigured. It is the same shape as the w132 defect Rule 8 cites, in the one
channel w132 never probed, because every existing parity row sends a
well-formed body. It is NOT an `org_id` defect — a missing field or a non-object
body does it too — and closing it means a `RequestValidationError` handler on
`clinical/app.py`, which changes every route's answer and is a policy ruling
this wave was not given. `test_the_known_gap_*` in §2 is its tripwire, and its
docstring carries the whole finding.

WHY (a) IS TESTED BY COUNTING AND BY A STUB CLOCK AND NOT IN MILLISECONDS OF
REAL BCRYPT. `w132-http-auth-audit.md` §11 rules out "a test that MEASURES
login timing in milliseconds" as a flake on a shared runner, and a flaky
security test gets deleted. But "prove the change behaviourally, not by reading
the code" rules out an AST guard on its own. Both are satisfied by making the
work ITSELF observable: the hasher is a real object the method calls, so a
counting hasher observes what the method actually did at run time, and a
deliberately slow hasher makes the elapsed-time difference dominate everything
else in the request so the timing row measures the property and not the runner.
An AST row is included as well, but as the *third* line of defence and never
as the proof.

RULE 2a — THE METHOD, NOT A RAW QUERY. §1 and §2's bcrypt-count rows call
`DataAccess.login` / `DataAccess.patient_login`; §2's and §4's refusal rows go
through the real HTTP routes. Direct SQL appears only in fixtures that must
reach a state the clock cannot be asked for (a session past its window, a
disabled organisation). No assertion reads the database.

RULE 4 — EVERY ABSENCE AND EVERY SET HAS A POSITIVE TWIN. Each
`test_the_positive_twin_*` names, in its docstring, the wrong version it
catches and what it is vacuous against.
"""

from __future__ import annotations

import ast
import datetime as dt
import http.cookiejar
import io
import json
import os
import pathlib
import time
import urllib.request
import uuid

import pytest

# Imported under a different name ON PURPOSE: several rows below bind a loop
# variable called `email`, which is the natural name for what it holds, and a
# bare `import email.message` would be shadowed by it.
from email.message import Message as _RawHeaders

DSN = os.getenv("CLINICAL_TEST_DSN")
CLINICAL_ROOT = pathlib.Path(__file__).resolve().parent.parent

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
PASSWORD = "correct horse battery staple"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"
CLIENT_IP = "203.0.113.19"

_UNKNOWN_UUID = "00000000-0000-0000-0000-000000000000"

# The one refusal, in BOTH channels. A literal, not an import: renaming the
# constant must not be able to silently redefine what "indistinguishable"
# means.
THE_REFUSAL = (401, json.dumps({"detail": "Authentication failed."}, separators=(",", ":")).encode())

# The EIGHT authenticated STAFF routes. `POST /auth/logout` is in this tuple and
# that is the entire point of §4 — it is the one that was in no set anywhere in
# this repository, and it is easy to omit precisely because its success is a
# 204 rather than a 200.
#
# ── WIDENED IN w152, AND THE WIDENING IS THIS FILE'S OWN LESSON APPLIED. ─────
# It said FOUR, and four was every authenticated staff route when it was
# written. w152 mounted the administrative surface — four more routes, all
# staff-authenticated by the same `_get_session`. A refusal-parity set that
# names "every authenticated staff route" and silently means "the four that
# existed in 2026-09" is exactly the defect §4 exists to have closed: the
# docstring of `test_w132_login_refusal_parity.py::_authenticated_route_probes`
# records the same thing happening to it when the two logout routes landed.
#
# All eight are probed with the same eight INVALID credentials, so the
# administrative routes can reach neither their role gate nor their domain
# method here — the subject is the authentication refusal and nothing else.
# w154. The closed role vocabulary, IMPORTED rather than retyped — see
# `_shapes` for why a path segment can be one of these and what that means.
from clinical.data_access import ROLES as _ROLES  # noqa: E402

_ROLE_SEGMENTS = frozenset(_ROLES)

AUTHENTICATED_STAFF_ROUTES = (
    ("GET", "/exceptions/worklist?owner=someone", None),
    ("GET", f"/exceptions/orders/{_UNKNOWN_UUID}", None),
    ("PATCH", f"/exceptions/{_UNKNOWN_UUID}", {"resolution_action": "retry_check", "resolution_note": "x"}),
    ("POST", "/auth/logout", None),
    # w152. The administrative surface. MEASURED, not assumed: the session
    # dependency is solved before the request body is validated, so these
    # answer the authentication refusal even with no body at all — which is
    # what `_call` sends for a POST.
    ("POST", f"/admin/users/{_UNKNOWN_UUID}/roles", {"role": "Orderer"}),
    ("POST", f"/admin/users/{_UNKNOWN_UUID}/disabled", {"disabled": True}),
    ("POST", f"/admin/patient-logins/{_UNKNOWN_UUID}/disabled", {"disabled": True}),
    ("GET", "/admin/audit", None),
    # w154 (WAVE 4). Role revocation, the inverse of the assign route above.
    # A DELETE, and the first one in this set — `_call` grew a branch for it.
    # The role segment is a member of the closed seven-string vocabulary, not
    # an identifier, which is why `_shapes` has to reduce it; see there.
    ("DELETE", f"/admin/users/{_UNKNOWN_UUID}/roles/Approver", None),
    # w155 (WAVE 5). Patient credential ISSUANCE. It belongs in this set for
    # the same reason as every row above and one of its own: it is the route
    # on this surface that NAMES A PATIENT, so a refusal that varied by
    # session-lifecycle state here would be the first staff refusal that
    # varied while a patient identifier was in the URL.
    ("POST", f"/admin/patients/{_UNKNOWN_UUID}/logins", {"email": "x@y.example", "password": "a password"}),
    # w156 (WAVE 6). Report retraction. It is the SECOND route on this surface
    # gated on a role that is not Administrator (`/admin/audit` is Auditor;
    # this one is Approver), which is why `_ADMIN_ROUTES_NOT_GATED_ON_ADMINISTRATOR`
    # below is a set rather than a single path comparison.
    ("POST", f"/admin/reports/{_UNKNOWN_UUID}/retraction", {"reason": "w156"}),
    # w158 (WAVE 8). The two laboratory-bench routes w157 mounted on the staff
    # session dependency. They belonged in this set the day they were served:
    # the derived comparison below reported them as unprobed, which is the
    # coverage hole this class exists to make impossible to keep.
    ("POST", f"/lab/orders/{_UNKNOWN_UUID}/samples", {"sample_type": "blood"}),
    ("POST", f"/lab/samples/{_UNKNOWN_UUID}/qc", {"qc_status": "pass"}),
    # w162. The three clinic routes -- the ordering clinician's surface,
    # `Orderer`-gated inside the domain method. They are here for the reason
    # every row above is, and the derived comparison below is once again what
    # said so: it reported them as unprobed the moment they were served, which
    # is this class's whole job. The consent route NAMES A PATIENT, so a
    # refusal that varied by session-lifecycle state on it would leak while a
    # patient identifier sat in the URL -- the same argument w155's row makes.
    ("POST", "/clinic/patients", {"name": "x", "dob": "1990-01-01", "sex": "F"}),
    ("POST", f"/clinic/patients/{_UNKNOWN_UUID}/consents", {"scope": "testing"}),
    ("POST", f"/clinic/consents/{_UNKNOWN_UUID}/withdrawal", None),
    # w164. TOTP ENROLMENT. It is here for the reason every row above is, and
    # the derived comparison below is once again what said so -- it reported
    # this route as unprobed the moment it was served, which is this class's
    # whole job.
    #
    # IT IS THE SHARPEST ROW IN THE SET, because the thing behind it is a
    # CREDENTIAL. A refusal that varied by session-lifecycle state here would
    # tell an anonymous or stale caller whether a named staff account exists,
    # on the one route whose success hands back a second-factor secret. It
    # takes NO BODY at all, so `_call` sends none -- which also makes it the
    # cleanest demonstration that the session dependency is solved before any
    # body is looked at.
    ("POST", f"/admin/users/{_UNKNOWN_UUID}/totp", None),
    # Stage 2. The six catalogue and order routes, here for the reason every
    # row above is: the derived comparison below reported them as unprobed the
    # moment they were served. Every case this class runs carries a dead or
    # absent session, so none of these can create a test or an order; the
    # bodies only have to be shaped well enough to reach the session
    # dependency, which is solved before any body is looked at.
    ("POST", "/admin/tests", {"name": "x", "assembly": "hg38", "genes": ["BRCA1"]}),
    ("POST", f"/admin/tests/{_UNKNOWN_UUID}/retirement", None),
    ("GET", "/clinic/tests", None),
    (
        "POST",
        "/clinic/orders",
        {
            "patient_id": _UNKNOWN_UUID,
            "test_id": _UNKNOWN_UUID,
            "consent_id": _UNKNOWN_UUID,
            "required_scope": "testing",
        },
    ),
    ("POST", f"/clinic/orders/{_UNKNOWN_UUID}/placement", None),
    ("POST", f"/clinic/orders/{_UNKNOWN_UUID}/cancellation", None),
    # Stage 3. The five review routes, here for the reason every row above is.
    # The cases in this class carry dead or absent sessions, so none of these
    # can record a claim or move a report; the bodies only have to be shaped
    # well enough to reach the session dependency, which is solved first.
    ("POST", f"/review/reports/{_UNKNOWN_UUID}/concurrence", {"interpretation_id": _UNKNOWN_UUID, "reason": "w139"}),
    (
        "POST",
        f"/review/reports/{_UNKNOWN_UUID}/disagreement",
        {"interpretation_id": _UNKNOWN_UUID, "variant_key": "k", "new_classification": "c", "reason": "w139"},
    ),
    ("POST", f"/review/reports/{_UNKNOWN_UUID}/submission", None),
    # The human-tool read API.
    ("GET", f"/review/reports/{_UNKNOWN_UUID}", None),
    ("GET", f"/review/reports/{_UNKNOWN_UUID}/revision", None),
    ("GET", f"/review/reports/{_UNKNOWN_UUID}/signoff-state", None),
    ("GET", "/auth/me", None),
    ("GET", "/auth/me/signing-identity", None),
    ("POST", f"/review/reports/{_UNKNOWN_UUID}/approval", None),
    ("POST", f"/review/reports/{_UNKNOWN_UUID}/release", None),
)

# The administrative paths whose role gate is NOT Administrator. The fixture
# principal in this file holds Administrator and nothing else, so these answer
# 403 (authenticated, wrong role) where the rest answer 404 (authenticated,
# right role, target does not exist in this organisation). Both are
# distinguishable from the anonymous 401, which is the property the set above
# is for.
_ADMIN_ROUTES_NOT_GATED_ON_ADMINISTRATOR = ("/admin/audit", "/retraction")


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
    """Ten attempts per source per minute is the shipped limit, and locking one
    account out costs five by itself. A 429 is not the refusal any row here is
    about — the limiter has its own file."""
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
    TWO live organisations and one disabled one, with the SAME address enrolled
    in both live organisations.

    The shared address is the fixture §2 could not be written without: it is
    what separates "the org_id is checked" from "the org_id is carried along
    and the address is unique anyway". Org A's password and Org B's password
    differ, so authenticating A's address with B's password against either org
    must fail, and A's own pair must succeed — which no single-org fixture can
    distinguish.

    The bystanders exist for Rule 4: `locked` is the account §1 locks out, and
    `staff`/`colleague` are the two that must keep working while it is locked.
    """
    org_a = dao.create_organisation("Org A")
    org_b = dao.create_organisation("Org B")

    staff_id = dao.create_user(org_a, "admin@a.example", PASSWORD)
    colleague_id = dao.create_user(org_a, "colleague@a.example", PASSWORD)
    locked_id = dao.create_user(org_a, "locked@a.example", PASSWORD)
    # THE SAME ADDRESS, IN BOTH ORGANISATIONS, WITH DIFFERENT PASSWORDS.
    shared_a_id = dao.create_user(org_a, "shared@both.example", PASSWORD)
    shared_b_id = dao.create_user(org_b, "shared@both.example", "a completely different password")

    admin_b_id = dao.create_user(org_b, "admin@b.example", PASSWORD)

    system_id = dao.create_user(org_a, "system@a.example", PASSWORD)
    disabled_id = dao.create_user(org_a, "disabled@a.example", PASSWORD)
    mfa_id = dao.create_user(org_a, "mfa@a.example", PASSWORD)

    with conn.cursor() as cur:
        for user_id, org_id in ((staff_id, org_a), (admin_b_id, org_b)):
            # w162. ORDERER ALONGSIDE ADMINISTRATOR: `admin@a.example`
            # registers the two patients below, and registration is now the
            # ordering clinician's (human ruling 2026-09-19). Roles are
            # additive; every claim in this file is about login refusals,
            # cookie policy and refusal parity, none of which reads a role.
            for role in ("Administrator", "Orderer"):
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (user_id, org_id, role, user_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
                )
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (colleague_id, org_a, staff_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
        )
        cur.execute("UPDATE users SET is_system_account = TRUE WHERE user_id = %s", (system_id,))
        cur.execute("UPDATE users SET disabled = TRUE WHERE user_id = %s", (disabled_id,))
        # The only way an MFA-enrolled account can exist here: `enrol_totp`
        # raises before it writes on every DataAccess this repository builds.
        cur.execute("UPDATE users SET totp_secret = 'not-a-real-secret' WHERE user_id = %s", (mfa_id,))
    conn.commit()

    staff = dao.login("admin@a.example", org_a, PASSWORD)

    disabled_org = dao.create_organisation("Disabled Org")
    with conn.cursor() as cur:
        cur.execute("UPDATE organisations SET disabled = TRUE WHERE org_id = %s", (disabled_org,))
    conn.commit()

    patient_id = dao.create_patient(staff, "Alice A", dt.date(1990, 1, 1), "F")
    patient_login_id = dao.enrol_patient_login(staff, patient_id, "alice@portal.example", PASSWORD)
    locked_patient_id = dao.create_patient(staff, "Carol C", dt.date(1990, 1, 1), "F")
    locked_patient_login_id = dao.enrol_patient_login(staff, locked_patient_id, "carol@portal.example", PASSWORD)
    conn.commit()

    return {
        "org_a": org_a,
        "org_b": org_b,
        "disabled_org": disabled_org,
        "staff": staff,
        "staff_id": staff_id,
        "colleague_id": colleague_id,
        "locked_id": locked_id,
        "shared_a_id": shared_a_id,
        "shared_b_id": shared_b_id,
        "system_id": system_id,
        "disabled_id": disabled_id,
        "mfa_id": mfa_id,
        "patient_id": patient_id,
        "patient_login_id": patient_login_id,
        "locked_patient_login_id": locked_patient_login_id,
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


def _post_login(client, path, org_id, email, password):
    _reset_throttle()
    return client.post(path, json={"email": email, "org_id": str(org_id), "password": password})


def _staff_session(client, org_id, email, password=PASSWORD):
    """Authenticate over HTTP and return the opaque id the browser was handed.

    Through the route, not by calling `DataAccess.login` in process: an id
    minted in process is not the credential a browser holds, and §4's subject
    is what happens at the route.
    """
    response = _post_login(client, "/auth/login", org_id, email, password)
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _patient_session(client, org_id, email, password=PASSWORD):
    response = _post_login(client, "/portal/login", org_id, email, password)
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _call(client, method, path, cookies, body=None):
    if method == "GET":
        return client.get(path, cookies=cookies)
    if method == "PATCH":
        return client.patch(path, json=body, cookies=cookies)
    # w154: the role-revocation route is a DELETE and declares no body at all,
    # so it is sent bodyless the way the bodyless POSTs are.
    if method == "DELETE":
        return client.delete(path, cookies=cookies)
    # w152: a POST with a declared body is now in the set, so the body is sent
    # when there is one. `None` stays a bodyless POST rather than becoming a
    # JSON `null`, because `/auth/logout` declares no body and a body it did
    # not ask for is a different request from the one this set has been
    # probing since w139.
    if body is None:
        return client.post(path, cookies=cookies)
    return client.post(path, json=body, cookies=cookies)


def _staff_authenticated_paths():
    """
    Every path the app SERVES whose dependant carries the staff session
    dependency. w152.

    DERIVED FROM THE APP, which is the whole point: `AUTHENTICATED_STAFF_ROUTES`
    is a hand-written list, and a hand-written list of "every authenticated
    staff route" has now fallen behind the surface twice in this repository
    (the two logout routes at w138, the four administrative routes at w152).
    Walking `app.routes` is Standing Rule 9 in its corrected form -- descend
    into the wrapper `include_router` leaves, or the walk reports two.
    """
    from clinical.app import app
    from clinical.endpoints import _get_session

    found = set()

    def walk(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
                continue
            children = getattr(route, "routes", None)
            if children:
                walk(children)
                continue
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            if _get_session in [d.call for d in dependant.dependencies]:
                found.add(route.path)

    walk(app.routes)
    assert found, "no staff-authenticated route was found -- this comparison would be vacuous"
    return found


def _shapes(paths):
    """
    A path reduced to its SHAPE: every `{param}` segment and every segment
    that is a UUID becomes `*`.

    So `/exceptions/orders/{order_id}` (what the app declares) and
    `/exceptions/orders/00000000-...` (what a probe sends) are the same route,
    which is what a coverage comparison needs to be about.

    w154 WIDENED THIS BY EXACTLY ONE CASE, and the widening is written down
    rather than folded in. `DELETE /admin/users/{user_id}/roles/{role}` has a
    second variable segment that is NOT an identifier: it is a member of the
    closed seven-string role vocabulary, so a probe necessarily sends a real
    role name where the app declares `{role}`, and without this the two sides
    would never compare equal. `ROLES` is imported rather than retyped, so a
    role added to the platform is covered without anybody editing this helper.

    THIS IS A COVERAGE COMPARISON AND NOTHING ELSE. What it could now fail to
    distinguish is a hypothetical route whose path segment is LITERALLY a role
    name from the parameterised one — a confusion with no consequence for the
    property being checked, which is "is every served staff route probed".
    """
    reduced = set()
    for path in paths:
        segments = []
        for segment in path.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                segments.append("*")
                continue
            if segment in _ROLE_SEGMENTS:
                segments.append("*")
                continue
            try:
                uuid.UUID(segment)
            except (ValueError, AttributeError):
                segments.append(segment)
            else:
                segments.append("*")
        reduced.add("/".join(segments))
    return reduced


def _age_the_staff_session(conn, session_id, *, past_absolute_cap=False, past_idle_window=False):
    """
    Move ONE staff session's windows into the past.

    A FIXTURE, NOT AN EXPECTATION. The route builds its own `DataAccess` per
    request with a `SystemClock`, so a fake clock cannot be injected from out
    here and the only honest way to reach "an hour later" is to move the row.
    Rule 2a forbids building the EXPECTATION from a raw query; it does not
    forbid reaching a state with one. Scoped to one session id on purpose — a
    helper that aged the whole table would silently satisfy every bystander
    twin below.
    """
    session_id = uuid.UUID(str(session_id))
    with conn.cursor() as cur:
        if past_absolute_cap:
            cur.execute(
                "UPDATE sessions SET expires_at = %s WHERE session_id = %s",
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1), session_id),
            )
        if past_idle_window:
            # expires_at is deliberately left in the future: a row that moved
            # both would pass against a service that had stopped checking the
            # idle window entirely.
            cur.execute(
                "UPDATE sessions SET last_activity_at = %s - (idle_timeout_minutes + 1) "
                "* INTERVAL '1 minute' WHERE session_id = %s",
                (dt.datetime.now(dt.timezone.utc), session_id),
            )
        assert cur.rowcount == 1, f"the fixture aged {cur.rowcount} rows, not exactly one"
    conn.commit()


class _CountingHasher:
    """
    A REAL hasher that also counts. Not a mock of the method under test: it
    wraps the genuine `BcryptHasher` the production code would use, so every
    verification the method performs really happens and is really counted.

    This is what makes §1 behavioural rather than a reading of the source. The
    method calls `self._hasher.verify`; this object observes those calls as
    they occur, at run time, through the constructor parameter production code
    already exposes (`DataAccess(..., password_hasher=...)`). Nothing is
    patched and nothing is monkeypatched.
    """

    def __init__(self, rounds=4):
        from clinical.data_access import BcryptHasher

        self._inner = BcryptHasher(rounds=rounds)
        self.calls = 0

    def hash(self, plaintext: str) -> str:
        return self._inner.hash(plaintext)

    def verify(self, plaintext: str, hashed: str) -> bool:
        self.calls += 1
        return self._inner.verify(plaintext, hashed)


class _SlowHasher:
    """
    A hasher whose every verification costs a fixed, large, *known* amount of
    wall-clock time.

    THIS IS HOW §1'S TIMING ROW AVOIDS BEING A FLAKE. The property under test
    is "a locked account does not cost measurably less than a wrong password".
    Measured against real bcrypt-4 on a shared CI runner, the signal is tens of
    milliseconds and the noise is the runner, which is exactly the test the
    w132 audit refused to write. Measured against a hasher whose verification
    costs 120ms by construction, the signal is 120ms per unit and the noise is
    a database round trip on loopback. The QUANTITY is stubbed; the CONTROL
    FLOW under test — whether the method reaches a verification at all on that
    branch — is the real thing.

    `verify` returns False always, so it can only be used on failure paths.
    """

    DELAY_SECONDS = 0.12

    def __init__(self):
        self.calls = 0

    def hash(self, plaintext: str) -> str:  # pragma: no cover - fixtures use bcrypt
        raise AssertionError("the slow hasher must never be used to create a credential")

    def verify(self, plaintext: str, hashed: str) -> bool:
        self.calls += 1
        time.sleep(self.DELAY_SECONDS)
        return False


class _MovableClock:
    """
    A clock the test can wind forward, through the constructor parameter
    `DataAccess` already exposes.

    Needed because the difference between the right fix and the WRONG one is
    only visible across time: both refuse a locked account now, and they differ
    in whether the account is still locked THIRTY MINUTES LATER. A test that
    cannot move the clock cannot see that, which is why an earlier draft of
    `test_the_equalisation_does_not_drive_the_counter_*` passed against the
    wrong version and was rewritten — measured, not assumed (wrong-first W3).
    """

    def __init__(self, start):
        self._now = start

    def now(self):
        return self._now

    def advance(self, minutes):
        self._now = self._now + dt.timedelta(minutes=minutes)
        return self._now


def _dao_with(conn, hasher, clock=None):
    from clinical.data_access import DataAccess

    return DataAccess(conn, clock=clock, password_hasher=hasher)


def _lock_out(dao, org_id, email):
    """
    Drive a REAL lockout: MAX_FAILED_LOGIN_ATTEMPTS wrong passwords through the
    method that owns the policy.

    NOT an `UPDATE users SET locked_until = ...`. The lockout is a behaviour of
    `login`, and a fixture that wrote the column would pass against a version
    that had stopped counting — which is the failure Rule 2a exists for, and it
    matters more here than usual because §1's whole subject is a branch that is
    only reached when that column is set.
    """
    from clinical.data_access import MAX_FAILED_LOGIN_ATTEMPTS, AuthenticationError

    for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
        with pytest.raises(AuthenticationError):
            dao.login(email, org_id, "definitely the wrong password")
    return MAX_FAILED_LOGIN_ATTEMPTS


def _lock_out_patient(dao, org_id, email):
    from clinical.data_access import MAX_FAILED_LOGIN_ATTEMPTS, AuthenticationError

    for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
        with pytest.raises(AuthenticationError):
            dao.patient_login(email, org_id, "definitely the wrong password")
    return MAX_FAILED_LOGIN_ATTEMPTS


def _failure_reasons_of(function_name):
    """
    The `reason=` literals a login method actually has, read out of its own
    source by AST.

    NOT `inspect.getsource(DataAccess.login)`: both methods carry `@auditable`
    and `@transactional`, so the class attribute is a decorator wrapper and
    `getsource` returns the DECORATOR's body, containing none of the reasons.
    w138 recorded that trap; this re-uses its shape rather than rediscovering
    it. The callers assert the expected LENGTH before comparing, so an empty
    census cannot pass vacuously.
    """
    module = ast.parse((CLINICAL_ROOT / "data_access.py").read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    assert len(functions) == 1, f"expected exactly one def {function_name} in data_access.py, found {len(functions)}"
    return {
        keyword.value.value
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant)
    }


# ═════════════════════════════════════════════════════════════════════════════
# §1 — ITEM (a). THE LOCKED-OUT BRANCH. A REAL DEFECT, FIXED IN BOTH PRINCIPALS.
# ═════════════════════════════════════════════════════════════════════════════


class TestEveryFailurePathSpendsTheSamePasswordWork:
    """
    THE SET-OF-SIZE-ONE FORM, APPLIED TO THE THIRD CHANNEL.

    Rule 8 says an oracle can live in the status channel or the body channel.
    Timing is the third, and the refusal-parity files cannot see it: a locked
    account and a wrong password already answered the identical
    `(401, {"detail": "Authentication failed."})` before this wave. What
    differed was the WORK, and the unit of the difference is a whole bcrypt
    operation at the shipped cost factor of 12 — not a cache-timing sliver.

    So the assertion has the same shape as the other two channels: collect the
    bcrypt verification count over EVERY failure reason the method has, and
    assert the set has size one. A per-case equality would pass against a
    seventh branch nobody listed, and the case list is therefore derived from
    the method's own source rather than typed out.
    """

    def test_the_census_of_staff_failure_reasons_is_the_eight_that_exist(self):
        """
        Non-vacuity, Rule 4. Without this row a renamed keyword makes the sets
        below empty and every count assertion passes against nothing.

        Catches: a `reason=` keyword renamed, or `login` split into two defs.
        Vacuous against: a reason that exists in the method but is unreachable.
        """
        assert _failure_reasons_of("login") == {
            "unknown_organisation",
            "organisation_disabled",
            "unknown_user",
            "locked_out",
            "invalid_password",
            "user_disabled",
            "system_account",
            "invalid_totp",
        }

    def test_the_census_of_patient_failure_reasons_is_the_six_that_exist(self):
        assert _failure_reasons_of("patient_login") == {
            "unknown_organisation",
            "organisation_disabled",
            "unknown_patient_login",
            "locked_out",
            "invalid_password",
            "patient_login_disabled",
        }

    def _staff_counts(self, conn, world):
        """
        Every one of `login()`'s eight failure reasons, driven through the
        method, each with its OWN counting hasher so the counts are per-call
        and not cumulative.

        `locked_out` is produced by five real wrong passwords through `login`
        itself, on a hasher that is not the one being measured.
        """
        from clinical.data_access import AuthenticationError, ConfigurationError

        lock_driver = _dao_with(conn, _CountingHasher())
        _lock_out(lock_driver, world["org_a"], "locked@a.example")

        cases = {
            "unknown_organisation": (_UNKNOWN_UUID, "admin@a.example", PASSWORD, None),
            "organisation_disabled": (world["disabled_org"], "admin@a.example", PASSWORD, None),
            "unknown_user": (world["org_a"], "nobody@a.example", PASSWORD, None),
            "locked_out": (world["org_a"], "locked@a.example", PASSWORD, None),
            "invalid_password": (world["org_a"], "admin@a.example", "wrong", None),
            "user_disabled": (world["org_a"], "disabled@a.example", PASSWORD, None),
            "system_account": (world["org_a"], "system@a.example", PASSWORD, None),
            "invalid_totp": (world["org_a"], "mfa@a.example", PASSWORD, None),
        }

        counts = {}
        for name, (org_id, email, password, totp) in cases.items():
            hasher = _CountingHasher()
            probe = _dao_with(conn, hasher)
            with pytest.raises((AuthenticationError, ConfigurationError)):
                probe.login(email, uuid.UUID(str(org_id)), password, totp)
            counts[name] = hasher.calls
        return counts

    def _patient_counts(self, conn, dao, world):
        from clinical.data_access import AuthenticationError

        lock_driver = _dao_with(conn, _CountingHasher())
        _lock_out_patient(lock_driver, world["org_a"], "carol@portal.example")

        revoked_patient_id = dao.create_patient(world["staff"], "Dave D", dt.date(1990, 1, 1), "M")
        revoked_login_id = dao.enrol_patient_login(world["staff"], revoked_patient_id, "dave@portal.example", PASSWORD)
        dao.set_patient_login_disabled(world["staff"], revoked_login_id, True)
        conn.commit()

        cases = {
            "unknown_organisation": (_UNKNOWN_UUID, "alice@portal.example", PASSWORD),
            "organisation_disabled": (world["disabled_org"], "alice@portal.example", PASSWORD),
            "unknown_patient_login": (world["org_a"], "nobody@portal.example", PASSWORD),
            "locked_out": (world["org_a"], "carol@portal.example", PASSWORD),
            "invalid_password": (world["org_a"], "alice@portal.example", "wrong"),
            "patient_login_disabled": (world["org_a"], "dave@portal.example", PASSWORD),
        }

        counts = {}
        for name, (org_id, email, password) in cases.items():
            hasher = _CountingHasher()
            probe = _dao_with(conn, hasher)
            with pytest.raises(AuthenticationError):
                probe.patient_login(email, uuid.UUID(str(org_id)), password)
            counts[name] = hasher.calls
        return counts

    def test_all_eight_staff_failure_reasons_spend_exactly_one_verification(self, conn, world):
        """
        THE ROW THIS WAVE EXISTS FOR, staff half.

        Catches: `login()` returning from the locked-out branch without
        `_equalise_password_work` — the state at `863f07e`, where this set is
        `{0, 1}` and `locked_out` is the 0. It also catches any FUTURE branch
        added above the password check that forgets the same call.

        Vacuous against: a method that spends one verification on every path
        and refuses every caller — `TestThePositiveTwins` below is what makes
        that fail.
        """
        counts = self._staff_counts(conn, world)
        assert len(counts) == 8, counts
        assert set(counts) == _failure_reasons_of("login"), (set(counts), _failure_reasons_of("login"))
        assert set(counts.values()) == {1}, (
            "the password work is not uniform across login()'s failure paths -- the cheap branch is "
            f"a remotely measurable oracle for the state it names: {counts}"
        )

    def test_all_six_patient_failure_reasons_spend_exactly_one_verification(self, conn, dao, world):
        """
        The patient half, treated together with the staff half exactly as Wave
        4 recorded it should be. Same wrong version, other principal.
        """
        counts = self._patient_counts(conn, dao, world)
        assert len(counts) == 6, counts
        assert set(counts) == _failure_reasons_of("patient_login"), counts
        assert set(counts.values()) == {1}, (
            f"the password work is not uniform across patient_login()'s failure paths: {counts}"
        )

    def test_the_successful_path_spends_the_same_one_verification(self, conn, world):
        """
        The set of size one is only worth something if the SUCCESS path is in
        it: a service that spent one unit on every refusal and two on success
        would still tell a caller which addresses are real.

        Catches: an implementation that re-verifies on success. Vacuous
        against: anything on the failure paths, which is the rows above.
        """
        staff_hasher = _CountingHasher()
        assert _dao_with(conn, staff_hasher).login("colleague@a.example", world["org_a"], PASSWORD) is not None
        assert staff_hasher.calls == 1

        patient_hasher = _CountingHasher()
        assert (
            _dao_with(conn, patient_hasher).patient_login("alice@portal.example", world["org_a"], PASSWORD) is not None
        )
        assert patient_hasher.calls == 1

    def test_a_locked_account_is_not_measurably_faster_in_wall_clock_time(self, conn, world):
        """
        THE ORACLE AS AN ATTACKER WOULD ACTUALLY USE IT — elapsed seconds, not
        a call count.

        The unit is stubbed to 120ms so the row measures the control flow and
        not the runner (see `_SlowHasher`). Before the fix, `locked_out`
        returned without ever reaching a verification, so it cost ~0 against a
        wrong password's ~120ms and this row fails by a factor of more than
        ten. The threshold is half a unit, which no amount of loopback jitter
        can cross in either direction.

        Catches: exactly the defect, in the channel the finding is stated in.
        Vacuous against: a real-bcrypt deployment where the two differ by some
        smaller constant — that is what the count rows are for, and why both
        forms are here.
        """
        from clinical.data_access import AuthenticationError

        _lock_out(_dao_with(conn, _CountingHasher()), world["org_a"], "locked@a.example")

        elapsed = {}
        for name, email in (("locked_out", "locked@a.example"), ("invalid_password", "colleague@a.example")):
            hasher = _SlowHasher()
            probe = _dao_with(conn, hasher)
            started = time.monotonic()
            with pytest.raises(AuthenticationError):
                probe.login(email, world["org_a"], "a wrong password")
            elapsed[name] = time.monotonic() - started
            assert hasher.calls == 1, f"{name} performed {hasher.calls} verifications, not one"

        floor = _SlowHasher.DELAY_SECONDS / 2
        assert elapsed["locked_out"] >= floor, (
            "a locked-out address is measurably cheaper than a wrong password -- an attacker who "
            f"locked an address can confirm it exists by timing alone: {elapsed}"
        )
        assert elapsed["invalid_password"] >= floor, elapsed


class TestThePositiveTwins:
    """
    Standing Rule 4. Every row in §1 is satisfied completely by a `login()`
    that burns one bcrypt and then refuses everybody, and by one that has
    stopped locking accounts out at all — which would be the WORST possible way
    to make the timing uniform, and the brief forbids it in as many words.
    """

    def test_the_positive_twin_the_lockout_still_refuses_a_correct_password(self, conn, world):
        """
        THE BOUNDARY THE BRIEF DRAWS: "do NOT weaken the existing account
        lockout policy."

        Catches: making the timing uniform by deleting the lockout branch, or
        by letting it fall through to the real hash comparison — after which
        the CORRECT password would succeed on a locked account.
        Vacuous against: a service that refuses everything.
        """
        from clinical.data_access import AuthenticationError

        probe = _dao_with(conn, _CountingHasher())
        attempts = _lock_out(probe, world["org_a"], "locked@a.example")
        assert attempts == 5

        with pytest.raises(AuthenticationError):
            probe.login("locked@a.example", world["org_a"], PASSWORD)

    def test_the_positive_twin_an_unlocked_account_still_authenticates(self, conn, world):
        """
        The other half of the same boundary: the equalisation must not have
        made every account refuse.

        Catches: a `_equalise_password_work` call placed above the real verify
        in a way that swallows the success path. Vacuous against: the lockout
        policy itself.
        """
        session = _dao_with(conn, _CountingHasher()).login("colleague@a.example", world["org_a"], PASSWORD)
        assert session.org_id == world["org_a"]

    def test_the_positive_twin_locking_one_account_locks_only_that_one(self, conn, world):
        """
        Catches: a lockout that is keyed on the organisation or on the address
        rather than on the user — under which "every failure costs one unit" is
        trivially true because every account is locked.
        """
        probe = _dao_with(conn, _CountingHasher())
        _lock_out(probe, world["org_a"], "locked@a.example")

        assert probe.login("admin@a.example", world["org_a"], PASSWORD) is not None
        assert probe.login("colleague@a.example", world["org_a"], PASSWORD) is not None

    def test_the_equalisation_does_not_drive_the_counter_or_extend_the_lockout(self, conn, world):
        """
        THE SPECIFIC WRONG FIX. `_equalise_password_work` was chosen over
        "let it fall through to the real verify" because falling through calls
        `_register_failed_attempt`, and a locked account whose counter is
        driven by every subsequent probe is an account an attacker can hold
        locked for ever at no cost. The brief calls that weakening the policy.

        THE ASSERTION IS TAKEN FROM THE METHOD, NOT THE COLUMN (Rule 2a): the
        clock is wound forward past the ORIGINAL lockout expiry and the account
        must then be able to LOG IN. If the probes had extended `locked_until`,
        it could not.

        IT NEEDS A MOVABLE CLOCK AND THAT WAS MEASURED, NOT ASSUMED. The first
        draft of this row aged `locked_until` by SQL relative to its current
        value, which is the same arithmetic whether or not it was extended —
        and wrong-first W3 showed it passing against the wrong version. The
        difference only exists across time, so the test has to move through it.

        Catches: a locked-out branch that reaches `_register_failed_attempt`,
        after which an attacker holds an account locked for ever at no cost.
        Vacuous against: a lockout that never happens at all — the twin above
        is what fails then.
        """
        from clinical.data_access import LOCKOUT_MINUTES, AuthenticationError

        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        clock = _MovableClock(start)
        probe = _dao_with(conn, _CountingHasher(), clock=clock)

        _lock_out(probe, world["org_a"], "locked@a.example")  # locked until start + 30

        # Just BEFORE the window ends, the attacker prods the locked account.
        clock.advance(LOCKOUT_MINUTES - 1)
        for _ in range(4):
            with pytest.raises(AuthenticationError):
                probe.login("locked@a.example", world["org_a"], "another wrong guess")

        # Just AFTER the original window ends. Under the wrong version the
        # probes above pushed the expiry out to start + 59 and this refuses.
        clock.advance(2)
        assert probe.login("locked@a.example", world["org_a"], PASSWORD) is not None, (
            "prodding a locked account extended its lockout -- an attacker can hold it locked for "
            "ever at no cost, which is a weakening of the lockout policy, not a fix to its timing"
        )


class TestTheEqualisationIsOnEveryRefusingBranchStructurally:
    """
    THE THIRD LINE OF DEFENCE, AND IT IS EXPLICITLY NOT THE PROOF.

    The behavioural rows above are the proof. This one exists because they can
    only see the branches a fixture can reach, and the failure this wave is
    about is a branch somebody adds LATER and does not think to probe. An AST
    row sees a new `raise AuthenticationError()` the moment it is written.

    THE RULE, STATED PRECISELY, because the obvious formulation is wrong and
    writing the wrong one first is how this row would have shipped vacuous.
    "Every branch that raises must spend password work IN ITS OWN BODY" is
    FALSE of correct code: `user_disabled`, `system_account` and `invalid_totp`
    all raise from branches that contain no verification at all, and they are
    correct because they sit AFTER `if not self._hasher.verify(...)` and so can
    only be reached by a request that already spent a full unit.

    The property is about the PATH, not the branch. So the rule is positional:

        every top-level `if` that raises AuthenticationError and appears
        BEFORE the password check must spend password work in its own body;
        everything at or after the password check has already spent it.

    That is exactly the shape of the defect this wave fixed — `locked_out` was
    the last branch before the password check and the only one of the four
    before it that did not equalise.
    """

    @staticmethod
    def _method(name):
        module = ast.parse((CLINICAL_ROOT / "data_access.py").read_text(encoding="utf-8"))
        found = [
            node
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]
        assert len(found) == 1, name
        return found[0]

    @staticmethod
    def _is_the_password_check(node):
        """`if not self._hasher.verify(password, password_hash):` — the pivot."""
        if not isinstance(node, ast.If):
            return False
        return any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "verify"
            for call in ast.walk(node.test)
            if isinstance(call, ast.Call)
        )

    @classmethod
    def _branches_before_the_password_check(cls, name):
        """
        Each top-level `if` that raises AuthenticationError and sits BEFORE the
        password check, paired with whether it spends password work.

        Returns `(pivot_index, branches)`. A method restructured so the pivot
        is not found returns `(None, ...)` and fails
        `test_the_pivot_and_the_branches_are_both_found`, so no assertion below
        can pass over an empty dict.
        """
        method = cls._method(name)
        pivot = next((i for i, node in enumerate(method.body) if cls._is_the_password_check(node)), None)
        branches = {}
        if pivot is None:
            return None, branches
        for index, node in enumerate(method.body[:pivot]):
            if not isinstance(node, ast.If):
                continue
            raises = any(
                isinstance(inner, ast.Raise)
                and isinstance(inner.exc, ast.Call)
                and isinstance(inner.exc.func, ast.Name)
                and inner.exc.func.id == "AuthenticationError"
                for inner in ast.walk(node)
            )
            if not raises:
                continue
            branches[f"{name}#if@{index}"] = any(
                isinstance(call.func, ast.Attribute) and call.func.attr in ("_equalise_password_work", "verify")
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
        return pivot, branches

    def test_the_pivot_and_the_branches_are_both_found(self):
        """
        Rule 4, twice over. Without the pivot check a restructured method makes
        every row below pass over an empty dict; without the count check a
        walker that silently matched nothing would do the same.

        FOUR is the number of top-level refusing branches that precede the
        password check in `login` (unknown organisation, disabled organisation,
        unknown user, locked out) and in `patient_login` (the same four, with
        `unknown_patient_login` in place of `unknown_user`). If a fifth is
        added, the row below covers it automatically; if one is REMOVED, this
        row fails and asks why.
        """
        for name in ("login", "patient_login"):
            pivot, branches = self._branches_before_the_password_check(name)
            assert pivot is not None, f"the password check was not found in {name} -- this guard reads nothing"
            assert len(branches) == 4, (name, branches)

    def test_every_branch_before_the_password_check_in_login_spends_the_work(self):
        _, branches = self._branches_before_the_password_check("login")
        assert all(branches.values()), (
            f"a branch of login() refuses before the password check without spending the work: {branches}"
        )

    def test_every_branch_before_the_password_check_in_patient_login_spends_the_work(self):
        _, branches = self._branches_before_the_password_check("patient_login")
        assert all(branches.values()), (
            f"a branch of patient_login() refuses before the password check without spending the work: {branches}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# §2 — ITEM (b). `org_id` IN THE LOGIN BODY. VERDICT: NOT A DEFECT.
# ═════════════════════════════════════════════════════════════════════════════


class TestTheOrgIdInTheLoginBodyIsLoadBearingAndNotAnOracle:
    """
    WHAT THE FIELD IS FOR, PROVED RATHER THAN ASSERTED. `users` and
    `patient_logins` are unique on `(org_id, email)`, not on `email`. Both
    methods therefore look the principal up with `WHERE org_id = %s AND
    email = %s`. Remove the field and the lookup keys on the address alone,
    which authenticates one tenant's user against another tenant's credential —
    the exact defect the schema's composite unique exists to prevent.

    WHETHER IT CAN ENUMERATE. Three channels:

      STATUS and BODY — every combination of (org exists / disabled / absent)
      x (credentials right / wrong) is collected into one set here, both
      principals, through the real routes.

      TIMING — every one of those combinations is counted for bcrypt work in
      the same shape, because the unknown-organisation and disabled-
      organisation branches are the two places the equalisation is applied to
      something OTHER than a user lookup, and if either had been missed, a
      wrong org_id would be cheap and organisations would be enumerable one
      request at a time.
    """

    def _org_credential_matrix(self, client, world):
        """
        The (organisation, credential) grid, for both principals. Every cell is
        a refusal; the twins below are the cells that are not.

        The two rows that matter most are the ones the brief names: (wrong org,
        RIGHT credentials) and (right org, WRONG credentials). The shared
        address gives the first one real teeth — `shared@both.example` exists
        in BOTH organisations with different passwords, so "Org B's password
        against Org A" is a genuinely live credential presented to the wrong
        tenant, not a guess.
        """
        a, b, dead, absent = world["org_a"], world["org_b"], world["disabled_org"], _UNKNOWN_UUID
        return {
            "staff | absent org, right credentials": _post_login(
                client, "/auth/login", absent, "admin@a.example", PASSWORD
            ),
            "staff | disabled org, right credentials": _post_login(
                client, "/auth/login", dead, "admin@a.example", PASSWORD
            ),
            "staff | other live org, right credentials for org A": _post_login(
                client, "/auth/login", b, "admin@a.example", PASSWORD
            ),
            "staff | right org, wrong password": _post_login(client, "/auth/login", a, "admin@a.example", "wrong"),
            "staff | right org, address that exists nowhere": _post_login(
                client, "/auth/login", a, "nobody@a.example", PASSWORD
            ),
            "staff | absent org, address that exists nowhere": _post_login(
                client, "/auth/login", absent, "nobody@a.example", PASSWORD
            ),
            "staff | shared address, org A, org B's password": _post_login(
                client, "/auth/login", a, "shared@both.example", "a completely different password"
            ),
            "staff | shared address, org B, org A's password": _post_login(
                client, "/auth/login", b, "shared@both.example", PASSWORD
            ),
            "patient | absent org, right credentials": _post_login(
                client, "/portal/login", absent, "alice@portal.example", PASSWORD
            ),
            "patient | disabled org, right credentials": _post_login(
                client, "/portal/login", dead, "alice@portal.example", PASSWORD
            ),
            "patient | other live org, right credentials for org A": _post_login(
                client, "/portal/login", b, "alice@portal.example", PASSWORD
            ),
            "patient | right org, wrong password": _post_login(
                client, "/portal/login", a, "alice@portal.example", "wrong"
            ),
            "patient | right org, address that exists nowhere": _post_login(
                client, "/portal/login", a, "nobody@portal.example", PASSWORD
            ),
        }

    def test_every_organisation_and_credential_combination_is_one_pair(self, client, world):
        """
        Catches: any future handler that validated the organisation before the
        credential and said so — a 404 "no such organisation", a 400, or a
        body that differed. Organisations would then be enumerable by anybody
        who can POST to the login route, with no credential at all.

        Vacuous against: a service that refuses every login, including correct
        ones. `test_the_positive_twin_*` is what fails then.
        """
        observed = {
            name: (response.status_code, response.content)
            for name, response in self._org_credential_matrix(client, world).items()
        }
        assert len(observed) == 13, "the matrix shrank -- this row would prove less than it claims"
        assert len(set(observed.values())) == 1, {k: (s, b[:120]) for k, (s, b) in observed.items()}
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_no_refusal_names_the_organisation_or_the_address(self, client, world):
        """
        The body channel alone. A set of size one is already broken by a body
        that differed between cells; this row additionally fails a future leak
        that appears in ALL of them and so keeps the set at size one — for
        instance a handler that echoed the submitted `org_id` back.
        """
        forbidden = [str(world["org_a"]), str(world["org_b"]), str(world["disabled_org"])]
        forbidden += ["admin@a.example", "alice@portal.example", "shared@both.example", "Org A", "Org B"]
        for name, response in self._org_credential_matrix(client, world).items():
            for leak in forbidden:
                assert leak not in response.text, f"{name} leaks {leak!r}: {response.text}"

    def test_a_wrong_organisation_costs_the_same_password_work_as_a_wrong_password(self, conn, world):
        """
        THE TIMING CHANNEL FOR ITEM (b), measured the same way §1 measures its
        own. This is the row that decides "usable oracle" for the org field:
        even with status and body identical, an organisation that cost zero
        bcrypt would be enumerable by timing.

        Catches: a future `login()` that returned early on an unknown or
        disabled organisation without equalising — the exact shape that WAS
        wrong on the locked-out branch, on a different early return.
        Vacuous against: the status and body channels, which the rows above
        own.
        """
        from clinical.data_access import AuthenticationError

        cases = {
            "absent org, right credentials": (_UNKNOWN_UUID, "admin@a.example", PASSWORD),
            "disabled org, right credentials": (world["disabled_org"], "admin@a.example", PASSWORD),
            "other live org, right credentials for org A": (world["org_b"], "admin@a.example", PASSWORD),
            "right org, wrong password": (world["org_a"], "admin@a.example", "wrong"),
            "right org, unknown address": (world["org_a"], "nobody@a.example", PASSWORD),
        }
        counts = {}
        for name, (org_id, email, password) in cases.items():
            hasher = _CountingHasher()
            with pytest.raises(AuthenticationError):
                _dao_with(conn, hasher).login(email, uuid.UUID(str(org_id)), password)
            counts[name] = hasher.calls

        assert len(counts) == 5, counts
        assert set(counts.values()) == {1}, f"the organisation field is a timing oracle: {counts}"

    def test_a_malformed_org_id_discloses_nothing_about_any_ACCOUNT(self, client, world):
        """
        THE ONE PLACE THE LOGIN ROUTES DO NOT ANSWER 401, STATED RATHER THAN
        HIDDEN. `org_id` is typed `uuid.UUID`, so a value that is not a UUID is
        refused by FastAPI's request validation with a 422 before the handler
        body runs at all.

        THE QUESTION ITEM (b) ASKS IS WHETHER THE FIELD CAN ENUMERATE
        ORGANISATIONS OR USERS, AND THIS ROW ANSWERS IT: the 422 is a function
        of the caller's OWN input and of nothing in the database. It is
        byte-identical for an address that exists and one that exists nowhere,
        for a real organisation-shaped string and a nonsense one, and on both
        login routes. Nothing about any account, any organisation or any
        tenant can be read off it.

        WHAT IT DOES NOT SAY is that the 422 is harmless in every respect —
        see `test_the_known_gap_*` immediately below, which is a different
        question with a different answer and is carded rather than fixed.

        Catches: a validator that consulted the database before deciding, so
        that malformed-input handling became account-dependent.
        Vacuous against: the database-state channel, which is the next row's.
        """
        observed = {}
        for path in ("/auth/login", "/portal/login"):
            for label, address in (
                ("an address that exists", "admin@a.example"),
                ("an address that exists nowhere", "nobody@a.example"),
                ("a patient address", "alice@portal.example"),
            ):
                for value in ("not-a-uuid", "Org A", ""):
                    _reset_throttle()
                    response = client.post(path, json={"email": address, "org_id": value, "password": PASSWORD})
                    observed[f"{path} | {label} | {value!r}"] = (response.status_code, response.content)

        assert len(observed) == 18, observed
        statuses = {status for status, _ in observed.values()}
        assert statuses == {422}, observed

        # The bodies differ only in the caller's OWN echoed input, never in
        # anything from the database. Asserted by exclusion, on content.
        for name, (_, body) in observed.items():
            for leak in (
                str(world["org_a"]),
                str(world["org_b"]),
                str(world["disabled_org"]),
                "Org B",
                "CLINICAL_DSN",
                "postgresql://",
                "Traceback",
                "password_hash",
            ):
                assert leak.encode() not in body, f"{name} leaks {leak!r}: {body!r}"

    def test_the_known_gap_a_malformed_body_still_discriminates_DATABASE_state(self, client, monkeypatch):
        """
        A TRIPWIRE FOR A FINDING THIS WAVE REPORTS AND DOES NOT FIX. Read the
        docstring before reading the assertions: this row asserts the CURRENT,
        DEFECTIVE behaviour on purpose, so that the gap cannot be forgotten and
        so that closing it FAILS here and forces the closer to rewrite this row
        into the set-of-size-one form.

        THE FINDING (w139, carded C-17). An anonymous caller who sends a
        malformed BODY — not a malformed credential — reads the clinical
        database's reachability off the status code:

            database up          -> 422, with a pydantic error list
            database unreachable -> 401, the generic refusal
            DSN not configured   -> 401, the generic refusal

        That is the same shape as the w132 defect Rule 8 cites (the body is
        generic, the STATUS discriminates), surviving in the one channel w132
        never probed: every existing parity row sends a WELL-FORMED body, so
        the handler's dependencies always resolve and the 422 never appears.
        The cause is FastAPI's ordering — dependencies are solved before
        request-body validation errors are raised, so `_get_data_access`
        raises its 401 first whenever the database is not reachable.

        WHY IT IS NOT FIXED HERE. It is not an `org_id` defect: the same 401/422
        split appears for a MISSING field, for a wrong-typed `email`, and for a
        body that is not an object at all, on both login routes. Fixing it
        means a `RequestValidationError` handler on `clinical/app.py`, which
        changes the answer of EVERY route in the service and is a policy
        decision about whether honest clients keep their 422s. That is an
        app-level ruling of exactly the kind Wave 10 owns, it is not in Wave
        5's four items, and `clinical/app.py` installs no handlers or
        middleware at all today — a property its own structural guard pins.
        Inventing one to keep this wave tidy is the thing the queue forbids.

        Catches: nothing today. It is a REGRESSION TRIPWIRE, and the regression
        it watches for is somebody closing C-17 and leaving this document
        claiming the gap is open.
        """
        probes = {
            "malformed org_id": {"email": "admin@a.example", "org_id": "not-a-uuid", "password": PASSWORD},
            "missing fields": {"email": "admin@a.example"},
        }
        observed = {}
        for state, value in (
            ("up", DSN),
            ("unreachable", "postgresql://nobody:nobody@127.0.0.1:1/nothing"),
            ("unset", None),
        ):
            if value is None:
                monkeypatch.delenv("CLINICAL_DSN", raising=False)
            else:
                monkeypatch.setenv("CLINICAL_DSN", value)
            for name, body in probes.items():
                _reset_throttle()
                response = client.post("/auth/login", json=body)
                observed[f"{state} | {name}"] = (response.status_code, response.content)

        assert {status for key, (status, _) in observed.items() if key.startswith("up")} == {422}, observed
        assert {pair for key, pair in observed.items() if not key.startswith("up")} == {THE_REFUSAL}, observed
        assert len(set(observed.values())) > 1, (
            "C-17 appears to be CLOSED -- a malformed body no longer discriminates database state. "
            "That is good news and this row is now wrong: rewrite it as a set-of-size-one row over "
            "the three database states and strike C-17 from the audit."
        )

    def test_the_positive_twin_the_same_address_authenticates_only_in_its_own_organisation(self, client, world):
        """
        THE ROW THAT MAKES §2 MEAN SOMETHING. Every refusal above is satisfied
        perfectly by a login route that refuses everybody, and the specific
        wrong version this catches is the one the field exists to prevent:
        dropping `org_id` from the lookup so the address alone identifies the
        principal.

        `shared@both.example` exists in Org A and Org B with DIFFERENT
        passwords, so a lookup keyed on the address alone can satisfy at most
        one of the two pairs. Here each pair works against its own
        organisation.

        ── WHAT WRONG-FIRST W4 ACTUALLY MEASURED, recorded because it is more
        interesting than the row, and because the claim this docstring first
        made was wrong (Rule 3: fix the claim). Dropping `org_id` from the
        `users` lookup is caught DETERMINISTICALLY, but by
        `test_a_wrong_organisation_costs_the_same_password_work_as_a_wrong_
        password` and at the DATA LAYER, where `login("admin@a.example",
        org_b, <org A's password>)` stops raising AuthenticationError and
        raises `psycopg.errors.ForeignKeyViolation` instead.

        AT THE ROUTE IT IS INVISIBLE, AND THAT IS A PROPERTY WORTH KNOWING
        RATHER THAN A HOLE. `sessions` carries a COMPOSITE foreign key on
        `(user_id, org_id)` into `users`, so a session minted for a user in
        the wrong organisation cannot be inserted at all; and
        `auth_endpoints._refuse_uniformly` converts that failure into the same
        401 as every other. The address-only lookup therefore fails CLOSED
        over HTTP — it refuses legitimate logins non-deterministically (the
        row a seq scan returns first moves as Postgres rewrites tuples) but it
        never authenticates anybody across the tenant boundary. The schema is
        the backstop, and this row is not the thing that proves it.

        Vacuous against: the timing channel, and — as measured — against the
        address-only lookup at the HTTP layer.
        """
        a_cookie = _staff_session(client, world["org_a"], "shared@both.example", PASSWORD)
        b_cookie = _staff_session(client, world["org_b"], "shared@both.example", "a completely different password")
        assert a_cookie != b_cookie

        # And each credential really is a credential, at a real route.
        for cookie in (a_cookie, b_cookie):
            response = client.get("/exceptions/worklist?owner=someone", cookies={STAFF_COOKIE: cookie})
            assert (response.status_code, response.content) != THE_REFUSAL
            assert response.status_code == 200, response.text

    def test_the_positive_twin_a_correct_login_in_each_principal_is_not_refused(self, client, world):
        staff = _post_login(client, "/auth/login", world["org_a"], "admin@a.example", PASSWORD)
        assert staff.status_code == 204, staff.text
        patient = _post_login(client, "/portal/login", world["org_a"], "alice@portal.example", PASSWORD)
        assert patient.status_code == 204, patient.text


# ═════════════════════════════════════════════════════════════════════════════
# §3 — ITEM (c). `Secure` WITH NO TLS. VERDICT: NOT A CODE DEFECT.
# ═════════════════════════════════════════════════════════════════════════════


class TestTheSecureCookieFailsClosedOverPlainHttp:
    """
    THE QUESTION AND THE ANSWER.

    COUNTED at `863f07e`: nothing in this repository terminates TLS. The only
    two deployment shapes that exist are `clinical/Dockerfile` (ENTRYPOINT
    uvicorn, CMD `clinical.app:app --host 0.0.0.0 --port 8000`, with no
    `--ssl-certfile` / `--ssl-keyfile`) and the compose wiring held on
    `w125-clinical-service-up`, which publishes that same container's port 8000
    directly. There is no nginx, no ingress, no proxy of any kind in the tree.

    SO WHAT ACTUALLY HAPPENS, per deployment shape:

      * BROWSER over `http://` — the `Secure` attribute means the cookie is
        never SENT back over a non-secure scheme. The login answers 204, the
        browser holds (or drops) the cookie, and the very next request arrives
        anonymous and is refused. The service is UNUSABLE and it fails CLOSED.
        Nothing leaks: the credential is never transmitted over cleartext.
      * BROWSER over `https://` (a TLS terminator someone adds outside this
        repository) — the cookie is returned and everything works. This is the
        intended shape.
      * NON-BROWSER CLIENT (curl without a cookie jar, the `geper/review`
        CLI, `TestClient`) — `Secure` is a browser instruction and a bare HTTP
        client ignores it entirely. These are unaffected either way, which is
        why the whole clinical test suite passes over plain HTTP.

    THEREFORE IT IS NOT A DEFECT IN THIS CODE. Removing `secure=True` would be
    the defect: it would make the plain-HTTP deployment WORK, by putting a full
    bearer credential on the wire in cleartext on every request. The flag is
    what stops that. What is missing is a TLS terminator, which is a deployment
    decision this repository does not express — Wave 10's territory, already
    carded in `w138`'s C-12 paragraph and in the `_set_session_cookie`
    docstring, which states the consequence in as many words.

    HOW THIS IS SHOWN. Not by re-reading that docstring: by handing the REAL
    `Set-Cookie` header the route emits to Python's `http.cookiejar`, which is
    an independent implementation of the cookie specification, and asking it
    what a client would do with it. The assertion is the standard library's
    answer, not mine.
    """

    @staticmethod
    def _jar_for(set_cookie_header, url):
        """
        Feed one raw `Set-Cookie` header to a spec cookie policy, as a client
        at `url` would receive it. Returns the jar.
        """
        headers = _RawHeaders()
        headers["Set-Cookie"] = set_cookie_header
        request = urllib.request.Request(url)
        response = urllib.request.addinfourl(io.BytesIO(b""), headers, url, 200)
        jar = http.cookiejar.CookieJar()
        jar.extract_cookies(response, request)
        return jar

    @staticmethod
    def _would_send(jar, url):
        request = urllib.request.Request(url)
        jar.add_cookie_header(request)
        return request.get_header("Cookie")

    def _login_header(self, client, world):
        response = _post_login(client, "/auth/login", world["org_a"], "admin@a.example", PASSWORD)
        assert response.status_code == 204, response.text
        headers = response.headers.get_list("set-cookie")
        assert len(headers) == 1, headers
        return headers[0]

    def test_the_route_really_does_set_secure(self, client, world):
        """
        Non-vacuity, Rule 4. Every row below is about the consequence of the
        flag; without this one they would all pass on a cookie that never
        carried it.
        """
        header = self._login_header(client, world)
        assert "secure" in header.lower(), header
        assert "httponly" in header.lower(), header
        assert "samesite=strict" in header.lower(), header

    def test_a_spec_client_will_not_send_the_credential_over_plain_http(self, client, world):
        """
        THE CONSEQUENCE, from the standard library rather than from a comment.

        Catches: nothing in this repository — it is evidence for a VERDICT, and
        the verdict is that the flag behaves exactly as its docstring claims.
        It becomes a regression test the day somebody "fixes" the plain-HTTP
        deployment by dropping `secure=True`, because this row then fails and
        says why.
        """
        header = self._login_header(client, world)
        jar = self._jar_for(header, "http://clinical.example/auth/login")
        assert self._would_send(jar, "http://clinical.example/exceptions/worklist") is None, (
            "the session cookie would be transmitted over cleartext HTTP -- the session id IS the "
            "entire bearer credential"
        )

    def test_the_positive_twin_the_same_cookie_is_sent_over_https(self, client, world):
        """
        Without this, "the cookie is never sent" is satisfied perfectly by a
        cookie that is broken, mis-scoped, or not set at all — and the service
        would be unusable in EVERY deployment rather than only the plain-HTTP
        one.

        Catches: a malformed cookie, a wrong `Path`, a `Domain` nobody chose.
        Vacuous against: the `HttpOnly` and `SameSite` flags, which are the
        row above's.
        """
        header = self._login_header(client, world)
        jar = self._jar_for(header, "https://clinical.example/auth/login")
        sent = self._would_send(jar, "https://clinical.example/exceptions/worklist")
        assert sent is not None and STAFF_COOKIE in sent, (sent, header)

    def test_the_patient_cookie_behaves_identically_and_stays_on_its_path(self, client, world):
        """Both principals, because the finding is about the one helper that
        sets both. The path row is here rather than in a separate class because
        `Path=/portal` and `Secure` are decided in the same call, and a twin
        that only checked the flag would pass on a cookie scoped to `/`."""
        response = _post_login(client, "/portal/login", world["org_a"], "alice@portal.example", PASSWORD)
        assert response.status_code == 204, response.text
        header = response.headers.get_list("set-cookie")[0]
        assert "secure" in header.lower(), header

        http_jar = self._jar_for(header, "http://clinical.example/portal/login")
        assert self._would_send(http_jar, "http://clinical.example/portal/me") is None

        https_jar = self._jar_for(header, "https://clinical.example/portal/login")
        assert PATIENT_COOKIE in (self._would_send(https_jar, "https://clinical.example/portal/me") or "")
        # and NOT on a staff path, over https, which is what Path=/portal buys
        assert self._would_send(https_jar, "https://clinical.example/exceptions/worklist") is None

    def test_the_shipped_container_serves_plain_http_and_that_is_the_deployment_card(self):
        """
        THE CARD, AS AN EXECUTABLE FACT RATHER THAN A SENTENCE IN A DOCUMENT.

        `clinical/Dockerfile` launches uvicorn with no TLS arguments, so the
        container speaks plain HTTP and the `Secure` cookie it emits cannot be
        returned by a browser talking to it directly. Recording it here means
        that the day a TLS terminator IS added to this repository, this row
        fails and whoever added it is told to close the card rather than
        leaving a stale one open.

        Catches: the card going silently stale in either direction.
        Vacuous against: a TLS terminator added OUTSIDE this repository, which
        is precisely why this is a deployment card and not a code fix.
        """
        dockerfile = (CLINICAL_ROOT / "Dockerfile").read_text(encoding="utf-8")
        # Non-vacuity first: the matcher must find the launch that IS there.
        assert "uvicorn" in dockerfile, (
            "the Dockerfile no longer launches uvicorn -- this row is reading the wrong file"
        )
        assert "clinical.app:app" in dockerfile, dockerfile[-400:]
        for tls_argument in ("--ssl-certfile", "--ssl-keyfile", "--ssl-ca-certs"):
            assert tls_argument not in dockerfile, (
                f"{tls_argument} appeared in clinical/Dockerfile -- TLS now terminates inside this "
                "repository, so the Secure-cookie deployment card is closed and this row should be "
                "rewritten rather than deleted"
            )


# ═════════════════════════════════════════════════════════════════════════════
# §4 — ITEM (d). w138's C-16: `POST /auth/logout` IN THE REFUSAL SET AT LAST.
# ═════════════════════════════════════════════════════════════════════════════


class TestEveryStaffRefusalIsOnePairOnEveryRouteIncludingLogout:
    """
    RULE 8, WIDENED OVER THE STAFF SURFACE — the mirror of w138's
    `TestEveryPortalRefusalIsOnePairOnEveryRoute`, which did the patient half
    and left this one carded as C-16 because widening the staff refusal surface
    was not Wave 4's to change.

    COUNTED at `863f07e`: `POST /auth/logout` appeared in no refusal-parity set
    anywhere in this repository. `test_w132_logout.py` asserts two of its
    refusals CASE BY CASE (an anonymous caller, a patient cookie), and Rule 8
    is explicit that a per-case equality passes against a leak in a case nobody
    listed — which for this route is every session-lifecycle state, none of
    which was listed.

    Eight cases x TEN routes = 80 observations, ONE pair. (Four routes and
    32 observations until w152 mounted the administrative surface; eight and
    64 until w154 added role revocation; nine and 72 until w155 added patient
    credential issuance; the route list is derived from the app rather than
    trusted -- see `_staff_authenticated_paths`.)
    """

    def _cases(self, client, dao, conn, world):
        logged_out = _staff_session(client, world["org_a"], "colleague@a.example")
        assert client.post("/auth/logout", cookies={STAFF_COOKIE: logged_out}).status_code == 204

        administratively_ended = _staff_session(client, world["org_a"], "colleague@a.example")
        dao.terminate_session(world["staff"], uuid.UUID(administratively_ended))
        conn.commit()

        absolute = _staff_session(client, world["org_a"], "colleague@a.example")
        _age_the_staff_session(conn, absolute, past_absolute_cap=True)

        idle = _staff_session(client, world["org_a"], "colleague@a.example")
        _age_the_staff_session(conn, idle, past_idle_window=True)

        patient_cookie = _patient_session(client, world["org_a"], "alice@portal.example")

        return {
            "no credential at all": {},
            "a malformed id": {STAFF_COOKIE: "not-a-uuid"},
            "a well-formed id that names nothing": {STAFF_COOKIE: _UNKNOWN_UUID},
            "a genuine PATIENT session id": {STAFF_COOKIE: patient_cookie},
            "a session ended by logout": {STAFF_COOKIE: logged_out},
            "a session ended administratively": {STAFF_COOKIE: administratively_ended},
            "a session past its absolute cap": {STAFF_COOKIE: absolute},
            "a session past its idle window": {STAFF_COOKIE: idle},
        }

    def test_eight_cases_on_every_authenticated_staff_route_are_one_pair(self, client, dao, conn, world):
        """
        Catches: an oracle introduced on `POST /auth/logout` — the route in no
        set at `863f07e`. A logout that answered 204 to an unknown session id
        (telling a caller nothing exists by that name is harmless; telling them
        one DOES is not), or 404 to a terminated one, or 500 to an expired one,
        fails here and fails nowhere else in this repository.

        Vacuous against: a service that refuses every request, and against one
        that answers the WRONG user's data with a 200 — both are the twin's
        job.
        """
        cases = self._cases(client, dao, conn, world)
        assert len(cases) == 8, "the case list shrank -- this row would prove less than it claims"
        # w152: four, then eight. The number is a non-vacuity floor, not the
        # property -- the property is that the list matches what the app
        # serves, which the assertion below checks against the route census.
        # 10 at w155; 11 from w156, which added the report-retraction route;
        # 13 from w158, which added the two laboratory-bench routes; 16 from
        # w162, which added the three clinic routes; 17 from w164, which added
        # TOTP enrolment; 23 from Stage 2, which added the six catalogue and
        # order routes; 28 from Stage 3, which added the five review routes.
        # 33: the read-only sign-off state route joined the 32 (see the "human-tool read API" rows above).
        assert len(AUTHENTICATED_STAFF_ROUTES) == 33, "a staff route left the list without being probed"
        assert any(path.startswith("/auth/logout") for _, path, _ in AUTHENTICATED_STAFF_ROUTES), (
            "the logout route left the list -- it is the entire reason this class exists"
        )

        # w152. THE LIST IS NOW CHECKED AGAINST THE APP, NOT AGAINST ITSELF.
        # A count and a landmark are both satisfied by a list that has fallen
        # behind the surface -- which is precisely how this tuple came to say
        # FOUR while the app served eight. The staff-authenticated set is
        # DERIVED from the app: every served route whose dependant carries
        # `clinical.endpoints._get_session`. Both sides are reduced to their
        # SHAPE, so a concrete probe UUID and a `{param}` segment compare
        # equal and the comparison is about coverage, not spelling.
        assert _shapes(_staff_authenticated_paths()) == _shapes(
            path.split("?")[0] for _, path, _ in AUTHENTICATED_STAFF_ROUTES
        ), (sorted(_staff_authenticated_paths()), [p for _, p, _ in AUTHENTICATED_STAFF_ROUTES])

        observed = {}
        for name, cookies in cases.items():
            for method, path, body in AUTHENTICATED_STAFF_ROUTES:
                response = _call(client, method, path, cookies, body)
                observed[f"{name} | {method} {path}"] = (response.status_code, response.content)

        # eight cases across every staff route: 80 at w155, 88 from w156's
        # report-retraction route, 104 from w158's two bench routes, 128 from
        # w162's three clinic routes, 136 from w164's TOTP enrolment route,
        # 184 from Stage 2's six catalogue and order routes, 224 from Stage 3's
        # five review routes.
        # The product, not a hand-kept number.
        assert len(observed) == 8 * len(AUTHENTICATED_STAFF_ROUTES) == 264, observed
        assert len(set(observed.values())) == 1, {k: (s, b[:120]) for k, (s, b) in observed.items()}
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_no_refusal_on_any_staff_route_names_any_identifier(self, client, dao, conn, world):
        """
        The body channel on its own, across the whole staff surface. A set of
        size one is already broken by a body that differed; this row
        additionally fails a future leak that appears in ALL of them and so
        keeps the set at size one.
        """
        cases = self._cases(client, dao, conn, world)
        forbidden = [
            str(world["org_a"]),
            str(world["staff_id"]),
            str(world["colleague_id"]),
            "admin@a.example",
            "colleague@a.example",
            "alice@portal.example",
            "Org A",
        ]
        for name, cookies in cases.items():
            for method, path, body in AUTHENTICATED_STAFF_ROUTES:
                text = _call(client, method, path, cookies, body).text
                for leak in forbidden:
                    assert leak not in text, f"{name} | {method} {path} leaks {leak!r}: {text}"

    def test_the_positive_twin_a_live_staff_session_is_not_in_that_set_on_any_route(self, client, world):
        """
        Without this, "everything is the one refusal" is satisfied perfectly by
        a service that refuses everything, on all nine routes at once.

        Each route's OWN success shape is asserted, not merely "not the refusal
        pair", because three different 500s would satisfy that. The logout is
        LAST on purpose: it ends the very session the rows above it use, which
        is also a small proof that the preceding calls really were
        authenticated.

        NEITHER THIS ROW NOR THE SET ROW MAY BE DELETED WITHOUT THE OTHER.
        """
        cookie = _staff_session(client, world["org_a"], "admin@a.example")
        credential = {STAFF_COOKIE: cookie}

        worklist = client.get("/exceptions/worklist?owner=someone", cookies=credential)
        assert (worklist.status_code, worklist.content) != THE_REFUSAL
        assert worklist.status_code == 200, worklist.text
        assert worklist.json() == []

        # A 404 that NAMES the order, not the generic refusal. That difference
        # is the point: an authenticated caller is told what an anonymous one
        # is not, which is what makes the refusal set above meaningful.
        orders = client.get(f"/exceptions/orders/{_UNKNOWN_UUID}", cookies=credential)
        assert (orders.status_code, orders.content) != THE_REFUSAL
        assert orders.status_code == 404, orders.text
        assert _UNKNOWN_UUID in orders.text, orders.text

        patch = client.patch(
            f"/exceptions/{_UNKNOWN_UUID}",
            json={"resolution_action": "retry_check", "resolution_note": "x"},
            cookies=credential,
        )
        assert (patch.status_code, patch.content) != THE_REFUSAL
        assert patch.status_code == 404, patch.text

        # w152. THE ADMINISTRATIVE ROUTES, each with its OWN shape and
        # none of them the authentication refusal. `admin@a.example` holds
        # Administrator in this fixture and does NOT hold Auditor or Approver,
        # so the Administrator routes answer 404 (an authenticated caller,
        # naming a target that does not exist in its organisation) while the
        # audit route (Auditor) and, from w156, the retraction route
        # (Approver) answer 403 (authenticated, wrong role). Both are
        # distinguishable from the 401 an anonymous caller gets, which is what
        # makes the set above meaningful — and neither varies with anything
        # the caller is not entitled to know (that is proved in
        # test_w152_admin_surface.py::TestTheRefusalsCarryNoOracle).
        from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND

        for method, path, body in AUTHENTICATED_STAFF_ROUTES:
            if not path.startswith("/admin/"):
                continue
            if (method, path) == ("POST", "/admin/tests"):
                # Stage 2. The one /admin route in this set that names no
                # target: with a valid body an Administrator CREATES a catalogue
                # entry, so 201 is its success shape and 404 cannot be. Its
                # success is asserted in the block below, which also retires
                # the entry again -- the route is not skipped, it is held to
                # its own shape, and "not the refusal pair" still applies.
                continue
            response = _call(client, method, path, credential, body)
            assert (response.status_code, response.content) != THE_REFUSAL, (path, response.text)
            if any(marker in path for marker in _ADMIN_ROUTES_NOT_GATED_ON_ADMINISTRATOR):
                assert response.status_code == 403, response.text
                assert response.json() == {"detail": ADMIN_FORBIDDEN}, response.text
            else:
                assert response.status_code == 404, (path, response.text)
                assert response.json() == {"detail": ADMIN_NOT_FOUND}, response.text
                assert _UNKNOWN_UUID not in response.text, (
                    "the administrative 404 echoes the identifier it was asked about -- that is the "
                    "oracle the exception routes are allowed and this surface is not"
                )

        # w158 (WAVE 8). THE LABORATORY ROUTES, added to the set above because
        # w157 mounted them on the staff dependency and left them in no
        # refusal-parity set anywhere in this repository -- which the DERIVED
        # comparison at the top of this class caught, and which is the whole
        # reason that comparison is derived from the app rather than hand-kept.
        # `admin@a.example` holds Administrator and NOT Lab technician, so both
        # answer 403 (authenticated, wrong role) and reuse `admin_endpoints`'s
        # own constant -- no second refusal vocabulary. That is distinguishable
        # from the anonymous 401, which is what makes the set above meaningful.
        for method, path, body in AUTHENTICATED_STAFF_ROUTES:
            if not path.startswith("/lab/"):
                continue
            response = _call(client, method, path, credential, body)
            assert (response.status_code, response.content) != THE_REFUSAL, (path, response.text)
            assert response.status_code == 403, (path, response.text)
            assert response.json() == {"detail": ADMIN_FORBIDDEN}, (path, response.text)
            assert _UNKNOWN_UUID not in response.text, (path, response.text)

        # Stage 2. THE CATALOGUE AND ORDER ROUTES, each held to its OWN
        # success or refusal shape rather than to a shared one. `admin@a.example`
        # holds Administrator AND Orderer (see the fixture), so: creating a test
        # succeeds (201) and the entry can be retired (204); the catalogue read
        # is open to it (200); and the three order routes, which name patients,
        # tests, consents or orders that exist nowhere in this organisation,
        # answer the one administrative 404 with `admin_endpoints`'s own
        # constant and echo nothing -- authenticated, right role, no such
        # target, and distinguishable from the anonymous 401. The wrong-role
        # 403 on these routes is proved for every other role in
        # test_w166_catalogue_and_orders.py::TestOrdersRefuseEveryoneElse.
        created = _call(
            client, "POST", "/admin/tests", credential, {"name": "w139 twin", "assembly": "hg38", "genes": ["BRCA1"]}
        )
        assert (created.status_code, created.content) != THE_REFUSAL
        assert created.status_code == 201, created.text
        test_id = created.json()["test_id"]

        listed = _call(client, "GET", "/clinic/tests", credential, None)
        assert (listed.status_code, listed.content) != THE_REFUSAL
        assert listed.status_code == 200, listed.text
        assert test_id in [entry["test_id"] for entry in listed.json()]

        retired = _call(client, "POST", f"/admin/tests/{test_id}/retirement", credential, None)
        assert retired.status_code == 204, retired.text

        for method, path, body in AUTHENTICATED_STAFF_ROUTES:
            if not path.startswith("/clinic/orders"):
                continue
            response = _call(client, method, path, credential, body)
            assert (response.status_code, response.content) != THE_REFUSAL, (path, response.text)
            assert response.status_code == 404, (path, response.text)
            assert response.json() == {"detail": ADMIN_NOT_FOUND}, (path, response.text)
            assert _UNKNOWN_UUID not in response.text, (path, response.text)

        logout = client.post("/auth/logout", cookies=credential)
        assert (logout.status_code, logout.content) != THE_REFUSAL
        assert logout.status_code == 204, logout.text

    def test_the_positive_twin_logging_one_browser_out_leaves_another_working(self, client, world):
        """
        Catches: a logout that terminates by user, by organisation, or by
        truncating the table — under which "every refusal is one pair" would be
        trivially true because nothing authenticates any more.
        """
        first = _staff_session(client, world["org_a"], "admin@a.example")
        second = _staff_session(client, world["org_a"], "admin@a.example")
        assert first != second

        assert client.post("/auth/logout", cookies={STAFF_COOKIE: first}).status_code == 204

        still_live = client.get("/exceptions/worklist?owner=someone", cookies={STAFF_COOKIE: second})
        assert (still_live.status_code, still_live.content) != THE_REFUSAL
        assert still_live.status_code == 200, still_live.text
