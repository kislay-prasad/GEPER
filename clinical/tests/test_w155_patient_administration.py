"""
clinical/tests/test_w155_patient_administration.py
──────────────────────────────────────────────────
w155 (WAVE 5) — PATIENT ENROLMENT AND CREDENTIAL OPERATIONS, through the
administrative surface, tested as BEHAVIOUR through the app the service
actually serves (`clinical.app`).

THE ROUTE THIS WAVE ADDS, and it is one:

    POST /admin/patients/{patient_id}/logins        enrol_patient_login

Its pair-mate has been mounted since w152 and is re-proved here END TO END
rather than at the domain layer, because the requirement is about sessions
and w152 proved it about a `DataAccess` call:

    POST /admin/patient-logins/{id}/disabled        set_patient_login_disabled

─── THE AUTHORISATION TENSION, RECORDED AND NOT REINTERPRETED ───

w152 STOPPED patient credential issuance and carded it, because
`GEPER_CLINICAL_PLATFORM_SPEC.md` §16 recommends "no patient access in v1"
and wiring the only method that can mint a portal credential would have
settled that product ruling BY IMPLEMENTATION. That was right. It is
superseded by the human's Wave 5 dispatch, which instructs the wiring in
terms and sets the eight requirements below — a ruling that was MADE, by the
person entitled to make it, rather than one an implementer inferred.

§16 still says what it says, and the EXPERT call-out about counselling
provision that accompanies it is still unanswered. Seven `/portal/` routes
and now one issuing route are mounted against it. That is recorded here and
in `clinical/admin_endpoints.py`'s module docstring, and it is not
reinterpreted further in either direction; resolving it is an edit to §16 by
whoever owns §16.

─── WHAT THIS FILE DOES NOT PROVE ───

IT DOES NOT PROVE THE ROUTE IS REACHABLE BY A REAL DEPLOYMENT, and two
separate things stop it, one inherited and one this wave's own:

  1. Every route on this surface needs an Administrator session, and
     `provision_organisation` — the bootstrap w153 built — has no production
     caller outside `data_access.py`. Every administrative route is exactly
     as reachable as an operator invoking that method directly. This wave's
     route does not change that in either direction.
  2. The route NAMES a `patient_id`, and no route on THIS surface can create
     one. When w155 ran, `create_patient` was STOPPED and carded, so an
     Administrator enrolling a patient had to be handed a patient record some
     other path made — and there was no such path. THAT HALF CHANGED AT w162:
     the human ruled that the ordering clinician owns registration, and
     `POST /clinic/patients` now creates one. It is still not a route on this
     surface and it still does not need an Administrator; what it means is
     that the patient an Administrator enrols can now be produced by a
     production path rather than only by a fixture. See
     `TestTheUngatedThreeAreStoppedAndNotWired` below, which was inverted
     rather than deleted, and
     `clinical/tests/test_w162_registration_and_consent.py`.

So: IMPLEMENTED + TESTED + NOT REACHABLE, in the report's own vocabulary.
The Administrator below is provisioned by INSERTING THE ROLE ROW DIRECTLY,
exactly as `test_identity.py::admin_session`, `test_w133_staff_revocation.py`
and `test_w152_admin_surface.py` already do. That is a FIXTURE, not a
bootstrap.

─── THE FIVE MEASURED HAZARDS THIS FILE IS SHAPED AROUND ───

1. `@auditable` CARRIES NO `functools.wraps`, so `inspect.signature()` on any
   decorated method returns `(self, *args, **kwargs)` and tells you nothing.
   Every structural claim here is read from the SYNTAX TREE, and each one has
   a NON-VACUITY TWIN. `TestTheGateRunsFirst` also demonstrates the wildcard
   itself, so that a future reader who reaches for `inspect` sees why not.
2. A REFUSAL ASSERTING ONLY "IT FAILED" IS GREEN AGAINST BROKEN CODE. Every
   refusal row asserts the exact (status, raw body) pair, and every one has a
   POSITIVE TWIN the broken state fails.
3. THE CROSS-ORGANISATION COLLISION IS CONSTRUCTED, never hoped for. Two
   random UUIDs never collide, so organisation B holds a REAL patient whose
   REAL id organisation A names, and B is read back afterwards and proved to
   have gained no credential.
4. REFUSAL ORDER LEAKS, so refusals are asserted as SETS OF SIZE ONE across
   every case in a class, in BOTH channels at once (Standing Rule 8) — never
   pairwise.
5. VALIDATION ORDER LEAKS TOO. A vocabulary or format check placed before the
   role gate answers to everyone. `TestTheGateRunsFirst` proves the gate is
   the domain method's first statement AND that a caller with the wrong role
   gets the identical 403 whatever it puts in the body.

─── THE WRONG-FIRST LIST, AND WHICH ROW CATCHES EACH ───

(A) ENROLMENT REACHABLE WITHOUT AN ADMINISTRATOR.
    Caught by `TestAdministratorOnlyEnrolment` — an authenticated Orderer and
    an authenticated Auditor, both holding real live sessions in the right
    organisation, are refused, and the credential is then proved not to
    exist. Paired with the positive twin: the Administrator succeeds and the
    issued credential really authenticates at `/portal/login`.
    VACUOUS AGAINST: a route with no authentication at all — that is
    `TestAdministratorOnlyEnrolment::test_an_unauthenticated_caller_is_refused`.

(B) THE PATIENT IDENTIFIER TAKEN FROM THE REQUEST AS AUTHORITY ON A PORTAL
    SURFACE.
    Caught by `TestThePatientIdentifierIsNeverAnAuthorityClaim` — every
    `/portal/` handler is read from the syntax tree and proved to take no
    patient identifier in any channel, with a twin that finds one when it
    exists; and behaviourally, the administrative route's identifier is
    proved to select only within the caller's own tenant.
    VACUOUS AGAINST: an administrative route naming a patient, which is
    LEGITIMATE and is the other shape — `TestCrossOrganisationEnrolmentIsRefused`
    is what constrains that one.

(C) CROSS-ORGANISATION ENROLMENT SUCCEEDING.
    Caught by `TestCrossOrganisationEnrolmentIsRefused`, on the constructed
    collision, with organisation B read back.
    VACUOUS AGAINST: a leak in the refusal text rather than in the write —
    `TestTheRefusalsCarryNoOracle` is that row.

(D) THE CREDENTIAL ISSUER NOT RECORDED, OR RECORDED AS THE PATIENT.
    Caught by `TestTheCredentialIssuerIsRecorded`: `enrolled_by` is asserted
    EQUAL to the acting Administrator and explicitly UNEQUAL to the patient,
    to the enrolled credential, and to the other organisation's
    Administrator. An equality alone is satisfied by a column that happens to
    hold the right value for the wrong reason; the inequalities are what make
    "recorded as the patient" a failure.

(E) REVOCATION THAT MARKS THE CREDENTIAL BUT LEAVES A LIVE SESSION WORKING.
    Caught by `TestRevocationInvalidatesLivePatientSessions`, with a LIVE
    PATIENT COOKIE obtained from `/portal/login` that fetches `/portal/me`
    successfully BEFORE the revocation and is refused with the identical
    anonymous refusal AFTER it. Without the "before" half the row is
    satisfied by a cookie that never worked.
    VACUOUS AGAINST: the residue w133 documented — a `disabled` flag written
    by some OTHER path, which this route is not. That is
    `test_w133d_writer_guard.py`'s subject and it is unchanged.

(F) A ROUTE THAT LETS A PATIENT ENROL THEMSELVES.
    Caught by `TestNoPublicPatientSelfRegistration`: the served surface is
    enumerated (Standing Rule 9's walker, not `@router.` decorators and not a
    naive `app.routes`), every route that can reach `enrol_patient_login` is
    proved to carry the STAFF session dependency, the production caller
    census of `enrol_patient_login` is taken by syntax tree and is exactly
    one, and a live PATIENT session id presented to the enrolment route is
    refused.
    VACUOUS AGAINST: a staff principal with the wrong role — that is (A).
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os
import pathlib

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
CLINICAL = REPO / "clinical"
ADMIN_MODULE = CLINICAL / "admin_endpoints.py"
DATA_ACCESS_MODULE = CLINICAL / "data_access.py"
PORTAL_MODULE = CLINICAL / "portal_endpoints.py"
SCHEMA_SQL = CLINICAL / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
STAFF_PASSWORD = "correct horse battery staple"
PATIENT_PASSWORD = "a sufficiently long patient password"
ISSUED_PASSWORD = "an issued patient password, long enough"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND  # noqa: E402
from clinical.data_access import GENERIC_AUTH_ERROR  # noqa: E402


def _body(detail):
    """The exact bytes FastAPI serialises `{"detail": ...}` to — raw, not a
    parsed dict, so that a refusal SET can be formed from it (Rule 8) and so
    that whitespace and key order are part of what is compared."""
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


UNAUTHENTICATED = (401, _body(GENERIC_AUTH_ERROR))
FORBIDDEN = (403, _body(ADMIN_FORBIDDEN))
NOT_FOUND = (404, _body(ADMIN_NOT_FOUND))

NOWHERE = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"


def _pair(response):
    """(status, raw body bytes) — BOTH channels as one hashable value."""
    return (response.status_code, response.content)


def _enrol(client, patient_id, credential=None, email="issued@portal.example", password=ISSUED_PASSWORD):
    return client.post(
        f"/admin/patients/{patient_id}/logins",
        json={"email": email, "password": password},
        cookies=credential or {},
    )


# ═══════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    """
    MODULE STATE, CLEARED BEFORE AND AFTER EVERY TEST. `_LOGIN_THROTTLE` is
    per-process and counts attempts by source address, and this file opens
    several patient and staff sessions per test. Without this a 429 arrives
    in the middle of a refusal-parity set and is indistinguishable from the
    401 the row is about — measured, not anticipated: three rows here failed
    that way first. The same fixture, for the same reason, is in
    `test_w132_staff_login_route.py`.
    """
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    yield
    _LOGIN_THROTTLE.reset()


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


def _grant(connection, user_id, org_id, roles):
    """The fixture-provisioned role assignment. A FIXTURE, not a bootstrap —
    the same break `test_identity.py::admin_session` already makes, and
    deliberately not something a deployment could use."""
    with connection.cursor() as cur:
        for role in roles:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, org_id, role, user_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc), None),
            )


@pytest.fixture()
def seeded(clinical_dsn):
    """
    TWO ORGANISATIONS, AND THE SECOND ONE IS THE POINT.

    Organisation B holds a REAL patient with no credential yet — that is the
    collision the cross-organisation row NAMES from organisation A, and the
    row it reads back to prove untouched. "Not yet enrolled" is deliberate:
    if B's patient already held a credential, a cross-tenant enrolment could
    be refused by the UNIQUE constraint rather than by the organisation
    predicate, and the row would be green for the wrong reason (w149's
    lesson, exactly).

    Organisation A holds:
      * patient_fresh — never enrolled. The positive twin's target.
      * patient_live  — enrolled, with a LIVE portal session, so revocation
                        has something to invalidate.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))

    org_a = dao.create_organisation("Org A")
    org_b = dao.create_organisation("Org B")

    admin_a = dao.create_user(org_a, "admin@a.example", STAFF_PASSWORD)
    auditor_a = dao.create_user(org_a, "auditor@a.example", STAFF_PASSWORD)
    orderer_a = dao.create_user(org_a, "orderer@a.example", STAFF_PASSWORD)
    admin_b = dao.create_user(org_b, "admin@b.example", STAFF_PASSWORD)
    # w162. ORGANISATION B NEEDS AN ORDERER TOO, because the patients this
    # fixture plants are now registered by the ordering clinician rather than
    # by an Administrator — see the note on the three `create_patient` calls
    # below. Org A already had one, as the wrong-role principal for the rows
    # about enrolment.
    orderer_b = dao.create_user(org_b, "orderer@b.example", STAFF_PASSWORD)

    _grant(connection, admin_a, org_a, ("Administrator",))
    _grant(connection, auditor_a, org_a, ("Auditor",))
    _grant(connection, orderer_a, org_a, ("Orderer",))
    _grant(connection, admin_b, org_b, ("Administrator",))
    _grant(connection, orderer_b, org_b, ("Orderer",))
    connection.commit()

    admin_a_session = dao.login("admin@a.example", org_a, STAFF_PASSWORD)
    auditor_a_session = dao.login("auditor@a.example", org_a, STAFF_PASSWORD)
    orderer_a_session = dao.login("orderer@a.example", org_a, STAFF_PASSWORD)
    admin_b_session = dao.login("admin@b.example", org_b, STAFF_PASSWORD)
    orderer_b_session = dao.login("orderer@b.example", org_b, STAFF_PASSWORD)

    # REGISTERED BY THE ORDERER, NOT BY THE ADMINISTRATOR (w162). These three
    # calls used to take `admin_a_session` / `admin_b_session`, which worked
    # because `create_patient` enforced no role at all. The human ruled on
    # 2026-09-19 that the ordering clinician owns registration, so the fixture
    # is given a correctly-roled session; the gate is not weakened and the
    # method is not bypassed. Every row in this file is about ENROLMENT, which
    # is still Administrator-only and unchanged — only the construction of the
    # patients those rows act on moved.
    patient_fresh = dao.create_patient(orderer_a_session, "Fiona Fresh", dt.date(1980, 1, 1), "F")
    patient_live = dao.create_patient(orderer_a_session, "Leo Live", dt.date(1975, 6, 15), "M")
    patient_b = dao.create_patient(orderer_b_session, "Bianca Bell", dt.date(1990, 3, 3), "F")

    # `patient_live` is enrolled through the DOMAIN METHOD in the fixture, so
    # that the revocation rows have a live session without depending on the
    # route they are not testing.
    login_live = dao.enrol_patient_login(admin_a_session, patient_live, "leo@portal.example", PATIENT_PASSWORD)

    connection.commit()
    connection.close()

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_a": admin_a,
        "auditor_a": auditor_a,
        "orderer_a": orderer_a,
        "admin_b": admin_b,
        "admin_a_cookie": {STAFF_COOKIE: str(admin_a_session.session_id)},
        "auditor_a_cookie": {STAFF_COOKIE: str(auditor_a_session.session_id)},
        "orderer_a_cookie": {STAFF_COOKIE: str(orderer_a_session.session_id)},
        "admin_b_cookie": {STAFF_COOKIE: str(admin_b_session.session_id)},
        "patient_fresh": patient_fresh,
        "patient_live": patient_live,
        "patient_b": patient_b,
        "login_live": login_live,
    }


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app)


@pytest.fixture()
def reader(clinical_dsn):
    """A SECOND, INDEPENDENT `DataAccess` for reading back what the routes
    did. Standing Rule 2a: state assertions go through the DOMAIN METHOD, not
    a hand-written SELECT that could omit the very `org_id` predicate this
    file exists to prove."""
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
    yield dao
    connection.close()


def _credential_exists(reader, org_id, email, password):
    """Does this credential authenticate? THROUGH `patient_login`, the method
    that actually decides, rather than through a row count — a row count is
    satisfied by a credential that exists and cannot be used."""
    from clinical.data_access import AuthenticationError

    try:
        return reader.patient_login(email, org_id, password) is not None
    except AuthenticationError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# THE ROUTE IS SERVED, AND IT AUTHENTICATES AS STAFF
# ═══════════════════════════════════════════════════════════════════════════


class TestTheEnrolmentRouteIsServed:
    def test_the_enrolment_route_is_on_the_served_surface(self):
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        assert ("/admin/patients/{patient_id}/logins", ("POST",)) in served_routes(app)

    def test_the_twin_the_walker_is_not_returning_a_wrapper_shaped_answer(self):
        """Standing Rule 9's correction, re-applied rather than recited: a
        walker that failed to descend into `_IncludedRouter` returns five
        objects, three of them `(None, ())`, and the row above would then be
        a failure for a reason that has nothing to do with this wave."""
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        census = served_routes(app)
        assert len(census) > 5, census
        assert all(path and path.startswith("/") for path, _ in census), census

    def test_the_enrolment_route_carries_the_staff_session_dependency(self):
        from clinical.admin_endpoints import router
        from clinical.endpoints import _get_session

        routes = [r for r in router.routes if getattr(r, "path", None) == "/admin/patients/{patient_id}/logins"]
        assert routes, [getattr(r, "path", None) for r in router.routes]
        for route in routes:
            dependencies = [d.call for d in getattr(route, "dependant").dependencies]
            assert _get_session in dependencies, (route.path, dependencies)

    def test_the_twin_the_dependency_check_can_see_an_unguarded_route(self):
        """Standing Rule 4: without this, the row above is satisfied by a
        check that finds nothing on any route."""
        from fastapi import APIRouter

        from clinical.endpoints import _get_session

        probe = APIRouter()

        @probe.post("/unguarded")
        async def unguarded() -> dict:
            return {}

        for route in probe.routes:
            dependencies = [d.call for d in getattr(route, "dependant").dependencies]
            assert _get_session not in dependencies, "the check cannot tell a guarded route from an open one"


# ═══════════════════════════════════════════════════════════════════════════
# (A) ADMINISTRATOR-ONLY ENROLMENT
# ═══════════════════════════════════════════════════════════════════════════


class TestAdministratorOnlyEnrolment:
    def test_an_administrator_enrols_and_the_issued_credential_really_authenticates(self, client, seeded, reader):
        """
        THE POSITIVE TWIN EVERY REFUSAL ROW BELOW NEEDS, and it asserts the
        EFFECT rather than the status: the credential the route issued is
        then used to authenticate. A 201 alone is satisfied by a handler that
        writes nothing.
        """
        assert not _credential_exists(reader, seeded["org_a"], "issued@portal.example", ISSUED_PASSWORD)

        response = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"])
        assert response.status_code == 201, response.text
        patient_login_id = response.json()["patient_login_id"]
        assert patient_login_id

        assert _credential_exists(reader, seeded["org_a"], "issued@portal.example", ISSUED_PASSWORD)

    def test_the_response_carries_the_identifier_and_nothing_else(self, client, seeded):
        """No email, no patient name, and above all no password echo."""
        response = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"])
        assert response.status_code == 201, response.text
        assert set(response.json()) == {"patient_login_id"}, response.json()
        assert ISSUED_PASSWORD.encode() not in response.content

    def test_an_orderer_and_an_auditor_are_refused_and_nothing_is_issued(self, client, seeded, reader):
        """
        (A). BOTH are authenticated, BOTH hold real live sessions in the right
        organisation, and neither is an Administrator. Asserted as a SET so
        that the two refusals cannot differ from each other.
        """
        refusals = {
            _pair(_enrol(client, seeded["patient_fresh"], seeded["orderer_a_cookie"])),
            _pair(_enrol(client, seeded["patient_fresh"], seeded["auditor_a_cookie"])),
        }
        assert refusals == {FORBIDDEN}, refusals
        assert not _credential_exists(reader, seeded["org_a"], "issued@portal.example", ISSUED_PASSWORD)

    def test_an_unauthenticated_caller_is_refused(self, client, seeded, reader):
        """The twin for the row above: without this, "a role is required" is
        satisfied by a route with no authentication at all."""
        refusals = {
            _pair(_enrol(client, seeded["patient_fresh"], {})),
            _pair(_enrol(client, seeded["patient_fresh"], {STAFF_COOKIE: MALFORMED})),
            _pair(_enrol(client, seeded["patient_fresh"], {STAFF_COOKIE: NOWHERE})),
        }
        assert refusals == {UNAUTHENTICATED}, refusals
        assert not _credential_exists(reader, seeded["org_a"], "issued@portal.example", ISSUED_PASSWORD)

    def test_a_live_patient_session_is_not_a_staff_credential(self, client, seeded, reader):
        """
        (F), on the credential channel. A PATIENT holding a perfectly valid
        live portal session cannot reach the enrolment route at all — the two
        session id spaces are different tables, so a patient session id is
        simply not found by `session_valid`.
        """
        opened = client.post(
            "/portal/login",
            json={"email": "leo@portal.example", "org_id": str(seeded["org_a"]), "password": PATIENT_PASSWORD},
        )
        assert opened.status_code == 204, opened.text
        patient_sid = opened.cookies[PATIENT_COOKIE]

        # It is a LIVE session — otherwise the refusal below is satisfied by
        # a credential that was never valid anywhere.
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: patient_sid}).status_code == 200

        assert _pair(_enrol(client, seeded["patient_fresh"], {STAFF_COOKIE: patient_sid})) == UNAUTHENTICATED
        assert not _credential_exists(reader, seeded["org_a"], "issued@portal.example", ISSUED_PASSWORD)


# ═══════════════════════════════════════════════════════════════════════════
# (F, validation order) THE GATE RUNS BEFORE ANYTHING ELSE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheGateRunsFirst:
    """
    THE HAZARD, STATED. A vocabulary or format check placed BEFORE the role
    gate answers to everyone: the refusal then varies with the CONTENT of the
    request at a point where the caller's authorisation has not been
    evaluated, and the 403 stops being a function of the caller's roles alone.
    That is Wave 4's wrong version F, and it is why the enrolment request
    model declares `str`/`str` and constrains neither.
    """

    @staticmethod
    def _first_gate(source, method_name):
        """The FIRST `self._require_role(...)` call inside a named method, and
        how many statements precede it — READ FROM THE SYNTAX TREE.

        NOT from `inspect`: `@auditable` carries no `functools.wraps`, so
        `inspect.signature` on any decorated method returns
        `(self, *args, **kwargs)` and `inspect.getsource` returns the
        wrapper's. The tree is the only instrument that sees the real body.

        Returns (role, index) or None.
        """
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != method_name:
                continue
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # the docstring is not a statement that can leak
            for index, stmt in enumerate(body):
                for call in ast.walk(stmt):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "_require_role"
                    ):
                        role = (
                            call.args[1].value
                            if len(call.args) > 1 and isinstance(call.args[1], ast.Constant)
                            else None
                        )
                        return (role, index)
            return None
        raise AssertionError(f"{method_name} not found in the tree at all")

    def test_the_domain_methods_first_statement_is_the_administrator_gate(self):
        source = DATA_ACCESS_MODULE.read_text(encoding="utf-8")
        assert self._first_gate(source, "enrol_patient_login") == ("Administrator", 0)

    def test_the_twin_the_analyser_reports_a_gate_that_is_not_first(self, tmp_path):
        """Standing Rule 4. Without this the row above passes against an
        analyser that always answers 0."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "class D:\n"
            "    def m(self, session, x):\n"
            '        """doc"""\n'
            "        if x not in ('a',):\n"
            "            raise ValueError(x)\n"
            "        self._require_role(session, 'Administrator')\n",
            encoding="utf-8",
        )
        assert self._first_gate(probe.read_text(encoding="utf-8"), "m") == ("Administrator", 1)

    def test_the_twin_the_analyser_reports_none_when_there_is_no_gate(self):
        """
        And the same instrument, on the same file, answers None for a method
        that has no gate at all.

        THE SUBJECT OF THIS ROW CHANGED AT w162, AND THAT IS THE POINT OF THE
        ROW RATHER THAN A WEAKENING OF IT. It used to name `create_patient`,
        `record_consent` and `withdraw_consent`, which were ungated when w155
        measured them; the human ruled on 2026-09-19 that the ordering
        clinician owns registration and consent, so all three are now
        `Orderer`-gated and can no longer serve as the negative control. The
        control moved to `create_interpretation` and `create_report`, which
        w157 measured as containing no `_require_role` anywhere and which are
        unchanged — carded there, not gated here.
        """
        source = DATA_ACCESS_MODULE.read_text(encoding="utf-8")
        assert self._first_gate(source, "create_interpretation") is None
        assert self._first_gate(source, "create_report") is None

    def test_inspect_signature_on_the_decorated_method_is_a_wildcard(self):
        """
        THE HAZARD DEMONSTRATED RATHER THAN DESCRIBED, so that a future
        reader who reaches for `inspect` sees the answer it gives. If
        `@auditable` ever gains `functools.wraps` this row fails, and that is
        correct: the guidance above would then be wrong.
        """
        from clinical.data_access import DataAccess

        parameters = list(inspect.signature(DataAccess.enrol_patient_login).parameters)
        assert parameters == ["self", "args", "kwargs"], parameters

    def test_the_request_model_constrains_no_value(self):
        """
        Read from the tree: `PatientLoginEnrolmentRequest`'s fields are bare
        `str` annotations with no default and no `Field(...)` constraint. A
        constraint here would be evaluated by Pydantic BEFORE the handler and
        therefore before the gate.
        """
        tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PatientLoginEnrolmentRequest":
                found = {
                    stmt.target.id: (ast.unparse(stmt.annotation), stmt.value and ast.unparse(stmt.value))
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                }
        assert found == {"email": ("str", None), "password": ("str", None)}, found

    def test_the_refusal_does_not_vary_with_the_body_for_a_caller_without_the_role(self, client, seeded):
        """
        THE BEHAVIOURAL HALF, and the one that would catch an `EmailStr`. An
        Orderer gets the IDENTICAL refusal for a well-formed body, a nonsense
        email, an empty password and an empty email — a set of size one. If
        any value check ran before the gate, one of these would be a 422 and
        the set would have two members.
        """
        bodies = [
            {"email": "fine@portal.example", "password": ISSUED_PASSWORD},
            {"email": "not-an-email-at-all", "password": ISSUED_PASSWORD},
            {"email": "fine@portal.example", "password": ""},
            {"email": "", "password": ""},
        ]
        refusals = {
            _pair(
                client.post(
                    f"/admin/patients/{seeded['patient_fresh']}/logins",
                    json=body,
                    cookies=seeded["orderer_a_cookie"],
                )
            )
            for body in bodies
        }
        assert refusals == {FORBIDDEN}, refusals

    def test_the_refusal_does_not_vary_with_the_target_for_a_caller_without_the_role(self, client, seeded):
        """The same claim on the other axis: a malformed id, an id that names
        nothing, a real patient in this organisation and a REAL PATIENT IN
        THE OTHER ONE are one refusal to a non-Administrator."""
        refusals = {
            _pair(_enrol(client, target, seeded["orderer_a_cookie"]))
            for target in (MALFORMED, NOWHERE, seeded["patient_fresh"], seeded["patient_b"])
        }
        assert refusals == {FORBIDDEN}, refusals


# ═══════════════════════════════════════════════════════════════════════════
# (D) THE CREDENTIAL ISSUER IS RECORDED — AND IS NOT THE PATIENT
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCredentialIssuerIsRecorded:
    def test_enrolled_by_is_the_acting_administrator_and_is_not_the_patient(self, client, seeded, clinical_dsn):
        """
        (D). THE EQUALITY AND THE THREE INEQUALITIES TOGETHER. An equality
        alone is satisfied by a column holding the right value for the wrong
        reason — a default, a copy of the patient id, a copy of the
        credential id. `enrolled_by` must be the ADMINISTRATOR WHO CALLED,
        and must not be any of the other identifiers in play.

        Read with SQL rather than a domain method for the honest reason that
        `patient_logins` has no reader method in `DataAccess` at all —
        Standing Rule 2a's target is a test that proves an ACCESS RULE by
        raw query, and this row proves a stored attribution. The access rules
        in this file are all proved through the methods. The audit row, which
        IS readable through a domain method, is the second, independent
        assertion of the same fact in the row below.
        """
        import psycopg

        response = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"])
        assert response.status_code == 201, response.text
        patient_login_id = response.json()["patient_login_id"]

        with psycopg.connect(clinical_dsn, autocommit=True, connect_timeout=10) as connection:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT enrolled_by, patient_id, org_id FROM patient_logins WHERE patient_login_id = %s",
                    (patient_login_id,),
                )
                enrolled_by, patient_id, org_id = cur.fetchone()

        assert enrolled_by == seeded["admin_a"], (enrolled_by, seeded["admin_a"])
        assert enrolled_by != patient_id, "the issuer is recorded as the patient -- wrong version (D)"
        assert str(enrolled_by) != patient_login_id, "the issuer is recorded as the credential"
        assert enrolled_by != seeded["admin_b"], "the issuer is the other organisation's administrator"
        assert org_id == seeded["org_a"], "the credential landed in the wrong organisation"

    def test_the_audit_row_names_the_acting_administrator_and_the_patient(self, client, seeded):
        """
        AUDIT ATTRIBUTION PRESERVED, read back THROUGH `GET /admin/audit` —
        the domain method — rather than by SQL. The actor is the
        Administrator; the organisation is A; the target named in the details
        is the PATIENT the credential was issued for.
        """
        response = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"])
        assert response.status_code == 201, response.text

        trail = client.get(
            "/admin/audit",
            params={"action": "patient_login_enrolled"},
            cookies=seeded["auditor_a_cookie"],
        )
        assert trail.status_code == 200, trail.text
        rows = trail.json()
        # TWO rows, and the second one is the fixture's own domain-level
        # enrolment of `patient_live`. Named rather than filtered away, so
        # that a handler which somehow attributed the ROUTE's enrolment to
        # the wrong patient could not hide inside a `[0]`.
        assert len(rows) == 2, rows
        assert {r["details"]["patient_id"] for r in rows} == {
            str(seeded["patient_fresh"]),
            str(seeded["patient_live"]),
        }, rows
        row = next(r for r in rows if r["details"]["patient_id"] == str(seeded["patient_fresh"]))
        assert row["action"] == "patient_login_enrolled"
        assert row["user_id"] == str(seeded["admin_a"]), row
        assert row["user_id"] != str(seeded["patient_fresh"]), row
        assert row["org_id"] == str(seeded["org_a"]), row
        assert row["details"]["patient_id"] == str(seeded["patient_fresh"]), row

    def test_a_refused_enrolment_is_attributed_to_the_caller_not_to_the_target(self, client, seeded):
        """
        The twin. `@auditable` records the DENIED outcome too, and it must be
        recorded against the principal who tried — never against the patient,
        and never in the organisation whose row was named.
        """
        assert _pair(_enrol(client, seeded["patient_b"], seeded["orderer_a_cookie"])) == FORBIDDEN

        trail = client.get("/admin/audit", cookies=seeded["auditor_a_cookie"])
        assert trail.status_code == 200, trail.text
        # Scoped to the TARGET the refused attempt named, so that the
        # fixture's own successful enrolment of `patient_live` — which is
        # attributed to the Administrator, correctly — cannot satisfy or
        # break this row.
        denied = [
            r
            for r in trail.json()
            if r["action"] == "patient_login_enrolled" and r["details"].get("patient_id") == str(seeded["patient_b"])
        ]
        assert denied, "the refused attempt left no audit row at all"
        for row in denied:
            assert row["user_id"] == str(seeded["orderer_a"]), row
            assert row["org_id"] == str(seeded["org_a"]), row


# ═══════════════════════════════════════════════════════════════════════════
# (C) CROSS-ORGANISATION ENROLMENT — THE COLLISION IS CONSTRUCTED
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossOrganisationEnrolmentIsRefused:
    """
    Two randomly generated UUIDs never collide, so a handler that dropped the
    organisation predicate would pass every other row in this file. The
    collision is therefore PLANTED: organisation B holds a real patient, not
    yet enrolled, and organisation A names its real id.
    """

    def test_an_administrator_cannot_enrol_a_real_patient_in_the_other_organisation(self, client, seeded, reader):
        response = _enrol(
            client,
            seeded["patient_b"],
            seeded["admin_a_cookie"],
            email="stolen@portal.example",
        )
        assert _pair(response) == NOT_FOUND, response.content

        # ORGANISATION B IS READ BACK. The refusal alone is satisfied by a
        # handler that wrote the row and then errored.
        assert not _credential_exists(reader, seeded["org_b"], "stolen@portal.example", ISSUED_PASSWORD)
        assert not _credential_exists(reader, seeded["org_a"], "stolen@portal.example", ISSUED_PASSWORD)

    def test_the_positive_twin_the_other_organisations_own_administrator_succeeds(self, client, seeded, reader):
        """
        THE ROW THAT MAKES THE ONE ABOVE NON-VACUOUS. The same patient, the
        same body, from B's own Administrator: it works. Without this, "org A
        cannot enrol patient B" is satisfied by a route that enrols nobody.
        """
        response = _enrol(
            client,
            seeded["patient_b"],
            seeded["admin_b_cookie"],
            email="bianca@portal.example",
        )
        assert response.status_code == 201, response.text
        assert _credential_exists(reader, seeded["org_b"], "bianca@portal.example", ISSUED_PASSWORD)

    def test_a_second_credential_for_one_patient_is_refused_after_the_gate(self, client, seeded):
        """
        409, and it is only ever visible to an Administrator naming a patient
        in their OWN organisation — the gate and the same-organisation read
        both run first. The refusal carries the domain's own message about
        proxy access rather than a message this router invented.
        """
        first = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"])
        assert first.status_code == 201, first.text

        second = _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"], email="again@portal.example")
        assert second.status_code == 409, second.text
        assert "proxy" in second.json()["detail"].lower(), second.json()

        # AND THE SAME REQUEST FROM A NON-ADMINISTRATOR IS STILL THE PLAIN
        # 403: the conflict is not an oracle for anyone who is not entitled
        # to it.
        assert _pair(_enrol(client, seeded["patient_fresh"], seeded["orderer_a_cookie"])) == FORBIDDEN

    def test_no_organisation_can_be_supplied_in_the_body_or_the_query_string(self, client, seeded, reader):
        """
        NO CALLER-CONTROLLED ORGANISATION ESCAPE. A body field is REFUSED
        (`extra="forbid"`) rather than silently ignored; a query parameter is
        ignored by construction and the write lands in the SESSION'S
        organisation, which is proved by reading both organisations back.
        """
        rejected = client.post(
            f"/admin/patients/{seeded['patient_b']}/logins",
            json={"email": "x@portal.example", "password": ISSUED_PASSWORD, "org_id": str(seeded["org_b"])},
            cookies=seeded["admin_a_cookie"],
        )
        assert rejected.status_code == 422, rejected.text

        ignored = client.post(
            f"/admin/patients/{seeded['patient_fresh']}/logins?org_id={seeded['org_b']}",
            json={"email": "q@portal.example", "password": ISSUED_PASSWORD},
            cookies=seeded["admin_a_cookie"],
        )
        assert ignored.status_code == 201, ignored.text
        assert _credential_exists(reader, seeded["org_a"], "q@portal.example", ISSUED_PASSWORD)
        assert not _credential_exists(reader, seeded["org_b"], "q@portal.example", ISSUED_PASSWORD)


# ═══════════════════════════════════════════════════════════════════════════
# THE REFUSALS CARRY NO ORACLE — SETS, NEVER PAIRWISE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheRefusalsCarryNoOracle:
    def test_the_404_is_one_object_over_malformed_absent_and_other_tenant(self, client, seeded):
        """
        Standing Rule 8, and hazard 4: refusal ORDER leaks, so this is a SET
        and not three comparisons. The third member is the one that matters
        and the one that cannot happen by accident.
        """
        refusals = {
            _pair(_enrol(client, target, seeded["admin_a_cookie"]))
            for target in (MALFORMED, NOWHERE, seeded["patient_b"])
        }
        assert refusals == {NOT_FOUND}, refusals

    def test_the_three_refusals_are_three_distinct_objects_and_no_more(self, client, seeded):
        """
        The same request differing only in WHO sent it. A fourth shape here
        would be a channel nobody reviewed. (400 is not on this route; 409
        and 422 are separately argued above and neither varies with the
        existence of any row outside the caller's tenant.)
        """
        assert {UNAUTHENTICATED, FORBIDDEN, NOT_FOUND} == {
            _pair(_enrol(client, NOWHERE, {})),
            _pair(_enrol(client, NOWHERE, seeded["orderer_a_cookie"])),
            _pair(_enrol(client, NOWHERE, seeded["admin_a_cookie"])),
        }


# ═══════════════════════════════════════════════════════════════════════════
# (E) REVOCATION INVALIDATES LIVE PATIENT SESSIONS — END TO END
# ═══════════════════════════════════════════════════════════════════════════


class TestRevocationInvalidatesLivePatientSessions:
    """
    THE MEASUREMENT THAT DECIDED THIS CLASS, taken at 60c48d9 before anything
    was built.

    `patient_session_valid` reads ONE `patient_sessions` row by session id and
    checks `terminated_at`, `expires_at` and the idle window. It does NOT join
    `patient_logins` and does NOT read `disabled`. So PATIENT SESSION
    VALIDATION DOES NOT CONSULT THE CREDENTIAL'S DISABLED STATE.

    That makes this the w133 `users.disabled` case and NOT the w154 role case.
    Roles resolve per request, so revoking one needs no session surgery;
    `disabled` is read by nothing on the authenticated path, so writing it
    invalidates nothing by itself and the termination must live in the domain
    method's body. It already does, since w133. NO SECOND MECHANISM WAS ADDED
    — a second one could only disagree with the first.

    What was missing was the PROOF the requirement actually asks for: the
    effect END TO END, over HTTP, with a live patient cookie. w152 proved it
    at the domain layer (`patient_session_valid` raises afterwards); this
    proves it at the surface a deployment has.
    """

    @staticmethod
    def _live_patient_cookie(client, seeded):
        opened = client.post(
            "/portal/login",
            json={"email": "leo@portal.example", "org_id": str(seeded["org_a"]), "password": PATIENT_PASSWORD},
        )
        assert opened.status_code == 204, opened.text
        return {PATIENT_COOKIE: opened.cookies[PATIENT_COOKIE]}

    def test_a_live_patient_cookie_works_before_and_is_refused_after(self, client, seeded):
        cookie = self._live_patient_cookie(client, seeded)

        # BEFORE. Without this half the row is satisfied by a cookie that
        # never worked at all.
        before = client.get("/portal/me", cookies=cookie)
        assert before.status_code == 200, before.text
        assert before.json()["patient_id"] == str(seeded["patient_live"]), before.json()

        revoked = client.post(
            f"/admin/patient-logins/{seeded['login_live']}/disabled",
            json={"disabled": True},
            cookies=seeded["admin_a_cookie"],
        )
        assert revoked.status_code == 204, revoked.text

        # AFTER. The SAME cookie. And the refusal is asserted as the anonymous
        # one in both channels, not merely as "not 200" -- a 500 here would be
        # a different defect wearing the same shape.
        after = client.get("/portal/me", cookies=cookie)
        assert _pair(after) == UNAUTHENTICATED, after.content

    def test_every_portal_route_refuses_the_revoked_cookie_as_one_answer(self, client, seeded):
        """
        The termination is keyed on the CREDENTIAL, so it is not a property of
        one route. All five patient read routes are asserted as a SET of size
        one afterwards — a route that had cached the session, or that read a
        different table, would be a second member.
        """
        cookie = self._live_patient_cookie(client, seeded)
        paths = ["/portal/me", "/portal/me/tests", "/portal/me/reports", "/portal/me/trends"]

        assert {client.get(path, cookies=cookie).status_code for path in paths} == {200}

        assert (
            client.post(
                f"/admin/patient-logins/{seeded['login_live']}/disabled",
                json={"disabled": True},
                cookies=seeded["admin_a_cookie"],
            ).status_code
            == 204
        )

        refusals = {_pair(client.get(path, cookies=cookie)) for path in paths}
        assert refusals == {UNAUTHENTICATED}, refusals

    def test_a_second_device_is_ended_too_because_the_unit_is_the_credential(self, client, seeded):
        """
        TWO live sessions from the SAME credential, as two devices would be.
        Revocation withdraws the thing that minted both, so ending only one
        would leave the revoked holder logged in on the other — the actor the
        revocation was invoked against.
        """
        first = self._live_patient_cookie(client, seeded)
        second = self._live_patient_cookie(client, seeded)
        assert first[PATIENT_COOKIE] != second[PATIENT_COOKIE], "the fixture minted one session, not two"
        assert {client.get("/portal/me", cookies=c).status_code for c in (first, second)} == {200}

        client.post(
            f"/admin/patient-logins/{seeded['login_live']}/disabled",
            json={"disabled": True},
            cookies=seeded["admin_a_cookie"],
        )

        refusals = {_pair(client.get("/portal/me", cookies=c)) for c in (first, second)}
        assert refusals == {UNAUTHENTICATED}, refusals

    def test_re_enabling_does_not_resurrect_the_ended_session(self, client, seeded):
        """
        A session ended by a revocation stays ended: the restored credential
        must be used to obtain a new one. This is the w133 ruling, held at the
        HTTP surface — and it is the row a "second mechanism" would break,
        because a validation-time `disabled` check would make the old cookie
        work again the instant the flag went back.
        """
        cookie = self._live_patient_cookie(client, seeded)
        assert client.get("/portal/me", cookies=cookie).status_code == 200

        for disabled in (True, False):
            assert (
                client.post(
                    f"/admin/patient-logins/{seeded['login_live']}/disabled",
                    json={"disabled": disabled},
                    cookies=seeded["admin_a_cookie"],
                ).status_code
                == 204
            )

        assert _pair(client.get("/portal/me", cookies=cookie)) == UNAUTHENTICATED

        # And the POSITIVE TWIN: the credential itself is live again, so the
        # row above is about the SESSION and not about a credential that
        # stayed broken.
        reopened = client.post(
            "/portal/login",
            json={"email": "leo@portal.example", "org_id": str(seeded["org_a"]), "password": PATIENT_PASSWORD},
        )
        assert reopened.status_code == 204, reopened.text
        assert client.get("/portal/me", cookies={PATIENT_COOKIE: reopened.cookies[PATIENT_COOKIE]}).status_code == 200

    def test_session_validation_still_does_not_read_the_disabled_column(self):
        """
        THE MEASUREMENT, PINNED SO THAT IT CANNOT CHANGE SILENTLY. If a later
        wave teaches `patient_session_valid` to join `patient_logins`, this
        row fails — and it SHOULD, because at that moment there are two
        mechanisms for one effect and the class docstring above stops being
        true. It is a trigger for a decision, not a prohibition.
        """
        source = DATA_ACCESS_MODULE.read_text(encoding="utf-8")
        body = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "patient_session_valid":
                body = ast.unparse(node)
        assert body is not None, "patient_session_valid is not in the tree"
        assert "patient_logins" not in body, body
        assert "disabled" not in body, body
        # The twin: the same instrument DOES see those words where they are.
        writer = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "set_patient_login_disabled":
                writer = ast.unparse(node)
        assert writer is not None and "patient_logins" in writer and "disabled" in writer


# ═══════════════════════════════════════════════════════════════════════════
# (B) THE PATIENT IDENTIFIER IS NEVER AN AUTHORITY CLAIM
# ═══════════════════════════════════════════════════════════════════════════


class TestThePatientIdentifierIsNeverAnAuthorityClaim:
    """
    TWO SHAPES, AND WHICH ONE EACH ROUTE IS.

      DEFENCE BY ABSENT PARAMETER — the portal. No `/portal/` handler takes a
      patient identifier in any channel, so the wrong version is UNWRITABLE.
      `/portal/me/reports/{report_id}` takes a RESOURCE id, never an identity
      one, and it is resolved against the session's own patient.

      AN AUTHENTICATED ADMINISTRATOR NAMING A TARGET — this wave's route. The
      identifier is in the path because administration acts on somebody else.
      What makes it safe is not its absence but that its ORGANISATION comes
      from `Session.org_id`, which `TestCrossOrganisationEnrolmentIsRefused`
      proves behaviourally.

    The shape that exists nowhere is the third: an unauthenticated request
    carrying a patient identifier.
    """

    @staticmethod
    def _identity_parameters(source):
        """Every handler parameter whose name looks like a PATIENT IDENTITY
        identifier, per handler, from the syntax tree."""
        found = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            named = [
                arg.arg
                for arg in node.args.args + node.args.kwonlyargs
                if arg.arg in ("patient_id", "patient", "patient_login_id", "subject_id")
            ]
            if named:
                found[node.name] = named
        return found

    def test_no_portal_handler_takes_a_patient_identifier(self):
        assert self._identity_parameters(PORTAL_MODULE.read_text(encoding="utf-8")) == {}

    def test_the_twin_the_scan_finds_one_where_one_exists(self):
        """Standing Rule 4, and it is not a synthetic probe: the SAME scan
        over the administrative module finds exactly the two handlers that
        legitimately name a patient target."""
        found = self._identity_parameters(ADMIN_MODULE.read_text(encoding="utf-8"))
        assert found == {
            "enrol_patient_login": ["patient_id"],
            "set_patient_login_disabled": ["patient_login_id"],
        }, found

    def test_a_patient_cannot_name_another_patient_through_the_portal(self, client, seeded):
        """
        BEHAVIOURAL. A query parameter inventing another patient is ignored by
        construction — the handler has nowhere to put it — so the answer is
        the caller's OWN record, not the named one.
        """
        opened = client.post(
            "/portal/login",
            json={"email": "leo@portal.example", "org_id": str(seeded["org_a"]), "password": PATIENT_PASSWORD},
        )
        assert opened.status_code == 204, opened.text
        cookie = {PATIENT_COOKIE: opened.cookies[PATIENT_COOKIE]}

        response = client.get(f"/portal/me?patient_id={seeded['patient_fresh']}", cookies=cookie)
        assert response.status_code == 200, response.text
        assert response.json()["patient_id"] == str(seeded["patient_live"]), response.json()
        assert response.json()["patient_id"] != str(seeded["patient_fresh"])


# ═══════════════════════════════════════════════════════════════════════════
# (F) NO PUBLIC PATIENT SELF-REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════


class TestNoPublicPatientSelfRegistration:
    @staticmethod
    def _production_callers(name):
        """Every PRODUCTION `ast.Call` of a named method — syntax tree, never
        grep. Grep credits a docstring as a caller, which is how
        `_release_report` came to be recorded with thirteen callers and zero
        calls."""
        found = []
        for path in REPO.rglob("*.py"):
            parts = path.parts
            if "tests" in parts or path.name.startswith("test_") or "__pycache__" in parts or ".git" in parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == name:
                    found.append((str(path.relative_to(REPO)).replace(os.sep, "/"), node.lineno))
        return sorted(found)

    def test_enrol_patient_login_has_exactly_one_production_caller_and_it_is_the_admin_route(self):
        callers = self._production_callers("enrol_patient_login")
        assert [path for path, _ in callers] == ["clinical/admin_endpoints.py"], callers

    def test_the_twin_the_census_finds_the_other_wired_methods_too(self):
        """Standing Rule 4: an analyser that found nothing would satisfy the
        row above. It finds the methods that ARE called, and finds none for a
        name nobody calls."""
        assert [p for p, _ in self._production_callers("set_patient_login_disabled")] == ["clinical/admin_endpoints.py"]
        assert self._production_callers("patient_login"), "the patient login route calls it; the census is blind"
        assert self._production_callers("no_such_method_anywhere") == []

    def test_no_unauthenticated_route_can_mint_a_patient_credential(self):
        """
        THE CENSUS, not an assertion. Every served route is enumerated with
        Standing Rule 9's walker; the only two that are unauthenticated by
        design are the two login routes and the two probes, and neither login
        route calls `enrol_patient_login`.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        unauthenticated = {"/auth/login", "/portal/login", "/healthz", "/readyz"}
        assert unauthenticated <= paths, sorted(paths)

        # Everything that mints a credential is on the ADMIN prefix, and the
        # admin prefix is entirely behind the staff dependency (proved route
        # by route in test_w152_admin_surface.py).
        # NEITHER LOGIN MODULE CALLS IT — and this is a CALL census, not a
        # substring search. Measured: `enrol_patient_login` appears as a WORD
        # in `clinical/portal_endpoints.py`, in prose, and a `not in source`
        # check therefore fails against correct code. That is exactly the
        # "grep credits a docstring as a caller" defect, inverted, and it is
        # written down here because this file made it once.
        minting = {path for path, _ in self._production_callers("enrol_patient_login")}
        assert minting == {"clinical/admin_endpoints.py"}, minting
        assert "clinical/auth_endpoints.py" not in minting
        assert "clinical/portal_endpoints.py" not in minting

    def test_every_served_route_but_the_five_public_ones_requires_a_session(self):
        """
        (F) AS A CENSUS OVER THE WHOLE SERVED SURFACE, not over the routes
        somebody remembered to list. Four routes are unauthenticated BY
        DESIGN — the two logins and the two probes — and every other route
        the app serves must carry a session dependency, staff or patient.

        THIS ROW EXISTS BECAUSE THE WRONG-FIRST RUN FOUND IT MISSING. A
        `POST /admin/self-register` with no dependency was added to
        `admin_endpoints.py` and this FILE stayed green: the caller census
        above did not see it, because a self-registration route need not call
        `enrol_patient_login` to be a self-registration route. Other files
        caught it (`test_w132_structural_guards.py`'s exact route census and
        `test_w152_admin_surface.py`'s per-route dependency check), which is
        the system working — but a file whose whole subject is "no public
        patient self-registration" should not depend on that.
        """
        from clinical.app import app
        from clinical.endpoints import _get_session
        from clinical.portal_endpoints import _get_patient_session

        # A FIFTH public route, declared: "/" is the static landing response (no data, no dependency).
        public = {"/", "/auth/login", "/portal/login", "/healthz", "/readyz"}
        unguarded = []
        seen = 0
        for route in app.routes:
            inner = getattr(route, "original_router", None)
            candidates = list(getattr(inner, "routes", [])) if inner is not None else [route]
            for candidate in candidates:
                path = getattr(candidate, "path", None)
                dependant = getattr(candidate, "dependant", None)
                if path is None or dependant is None:
                    continue
                seen += 1
                if path in public:
                    continue
                calls = {d.call for d in dependant.dependencies}
                if not calls & {_get_session, _get_patient_session}:
                    unguarded.append(path)
        assert seen >= 15, f"only {seen} routes inspected; the walker is blind"
        assert unguarded == [], unguarded

    def test_the_twin_the_session_census_sees_an_unguarded_route(self):
        """Standing Rule 4: the absence above is worthless if the walker
        cannot recognise a route with no dependency."""
        from fastapi import APIRouter

        from clinical.endpoints import _get_session
        from clinical.portal_endpoints import _get_patient_session

        probe = APIRouter()

        @probe.post("/self-register")
        async def self_register() -> dict:
            return {}

        for route in probe.routes:
            calls = {d.call for d in getattr(route, "dependant").dependencies}
            assert not calls & {_get_session, _get_patient_session}, calls

    def test_the_patient_facing_surface_serves_no_write_but_logout(self):
        """
        A self-registration route would be a POST on the patient surface. The
        only two that exist are `/portal/login` (authentication, which mints
        no credential — `patient_login` reads `patient_logins`, it never
        writes one) and `/portal/logout`.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        portal_writes = sorted(
            path for path, methods in served_routes(app) if path.startswith("/portal/") and "POST" in methods
        )
        assert portal_writes == ["/portal/login", "/portal/logout"], portal_writes


# ═══════════════════════════════════════════════════════════════════════════
# PATIENT IDENTITY REMAINS SEPARATE FROM STAFF IDENTITY
# ═══════════════════════════════════════════════════════════════════════════


class TestPatientIdentityRemainsSeparateFromStaff:
    def test_a_patient_credential_is_not_a_staff_credential_and_the_refusal_is_uniform(self, client, seeded):
        """
        The issued patient address presented to the STAFF login route is
        refused, and with the identical anonymous refusal an unknown address
        gets — so the staff surface does not disclose that the address exists
        as a patient.
        """
        assert _enrol(client, seeded["patient_fresh"], seeded["admin_a_cookie"]).status_code == 201

        refusals = {
            _pair(
                client.post(
                    "/auth/login",
                    json={"email": email, "org_id": str(seeded["org_a"]), "password": password},
                )
            )
            for email, password in (
                ("issued@portal.example", ISSUED_PASSWORD),
                ("nobody@nowhere.example", ISSUED_PASSWORD),
            )
        }
        assert refusals == {UNAUTHENTICATED}, refusals

    def test_a_staff_credential_is_not_a_patient_credential(self, client, seeded):
        """The mirror. And the positive twin, so neither row is satisfied by
        a login surface that refuses everyone."""
        refusals = {
            _pair(
                client.post(
                    "/portal/login",
                    json={"email": email, "org_id": str(seeded["org_a"]), "password": password},
                )
            )
            for email, password in (
                ("admin@a.example", STAFF_PASSWORD),
                ("nobody@nowhere.example", STAFF_PASSWORD),
            )
        }
        assert refusals == {UNAUTHENTICATED}, refusals

        assert (
            client.post(
                "/auth/login",
                json={"email": "admin@a.example", "org_id": str(seeded["org_a"]), "password": STAFF_PASSWORD},
            ).status_code
            == 204
        )

    def test_the_two_session_spaces_are_different_tables(self, client, seeded):
        """
        A patient session id in the staff cookie is refused; a staff session
        id in the patient cookie is refused; and both refusals are the same
        object as the absent-credential one. The separation is STRUCTURAL —
        two tables — not a check somebody remembered to write.
        """
        opened = client.post(
            "/portal/login",
            json={"email": "leo@portal.example", "org_id": str(seeded["org_a"]), "password": PATIENT_PASSWORD},
        )
        patient_sid = opened.cookies[PATIENT_COOKIE]
        staff_sid = seeded["admin_a_cookie"][STAFF_COOKIE]

        refusals = {
            _pair(_enrol(client, seeded["patient_fresh"], {STAFF_COOKIE: patient_sid})),
            _pair(client.get("/portal/me", cookies={PATIENT_COOKIE: staff_sid})),
            _pair(client.get("/portal/me", cookies={})),
        }
        assert refusals == {UNAUTHENTICATED}, refusals


# ═══════════════════════════════════════════════════════════════════════════
# WHAT THIS WAVE STOPPED — pinned so the cards cannot rot silently
# ═══════════════════════════════════════════════════════════════════════════


class TestTheUngatedThreeAreStoppedAndNotWired:
    """
    THE STOP w155 RECORDED HERE WAS LIFTED BY A RULING AT w162, AND THESE ROWS
    NOW PIN THE ANSWER INSTEAD OF THE QUESTION.

    What w155 measured at 60c48d9 was correct and is on the record: all three
    of `create_patient`, `record_consent` and `withdraw_consent` took a
    `Session` and bound `org_id` from it, none contained `_require_role`, and
    none had a production caller — so the gate would break no production
    caller, and WHICH ROLE it should demand was a product decision nobody had
    made. w155 refused to guess, which was the right call.

    The human ruled on 2026-09-19: "PATIENT REGISTRATION + CONSENT — the
    ORDERER/CLINICIAN owns both. Do NOT make Administrator registration
    mandatory. Do NOT make Lab Technician a patient identity creator." That
    ruling is exactly what this stop was waiting on. w162 implemented it —
    `_require_role(session, "Orderer")` as each method's first executable
    statement — and wired the three through `clinical/clinic_endpoints.py`.

    THE ROWS ARE INVERTED RATHER THAN DELETED, because the property w155 cared
    about is still worth pinning: it cared that no gate was invented WITHOUT a
    ruling and no route added WITHOUT one. The full behavioural suite for the
    new state is `clinical/tests/test_w162_registration_and_consent.py`; what
    is kept here is the narrow claim that the administrative surface did NOT
    acquire these capabilities, which is the half the ruling explicitly
    forbids.
    """

    def test_the_three_methods_are_now_gated_on_the_orderer(self):
        source = DATA_ACCESS_MODULE.read_text(encoding="utf-8")
        for name in ("create_patient", "record_consent", "withdraw_consent"):
            assert TestTheGateRunsFirst._first_gate(source, name) == ("Orderer", 0), name

    def test_the_three_methods_still_take_a_session_and_bind_the_organisation_from_it(self):
        """The half that was already true and must stay true: the gate is not
        a substitute for the tenant predicate, it is beside it. Read from the
        tree, because `inspect.signature` on a decorated method is a
        wildcard."""
        tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
        for name in ("create_patient", "record_consent", "withdraw_consent"):
            node = next(
                n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
            )
            assert [a.arg for a in node.args.args][:2] == ["self", "session"], name
            assert "session.org_id" in ast.unparse(node), name

    def test_the_only_route_reaching_any_of_the_three_is_the_clinic_router(self):
        """NOT the administrative surface. A route onto registration from
        `/admin` would encode the reading the ruling rejects."""
        callers = TestNoPublicPatientSelfRegistration._production_callers
        for name in ("create_patient", "record_consent", "withdraw_consent"):
            assert [path for path, _ in callers(name)] == ["clinical/clinic_endpoints.py"], (name, callers(name))

    def test_the_administrative_surface_has_no_patient_or_consent_write_route(self):
        """
        SCOPED TO `/admin`, WHICH IS WHAT THIS ROW'S NAME ALWAYS SAID. It used
        to assert that NO served path anywhere contained "consent", which was
        true when `/admin` and `/lab` were the only staff surfaces and became
        wrong the moment the ruling put consent on `/clinic`. The claim worth
        keeping is the one about the ADMINISTRATIVE prefix: the ruling forbids
        making Administrator registration mandatory, so registration and
        consent must not appear there.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        admin_paths = {path for path in paths if path.startswith("/admin")}
        assert admin_paths, "no /admin path at all -- this census is blind"
        assert "/admin/patients" not in admin_paths, admin_paths
        assert not any("consent" in path for path in admin_paths), admin_paths
        # The twin: the one patient-scoped admin path that DOES exist.
        assert "/admin/patients/{patient_id}/logins" in paths, paths
        # And the mirror twin: consent DOES live somewhere, on the clinician's
        # own prefix. Without this the row above is satisfied by a tree in
        # which the capability was deleted rather than moved.
        assert any(path.startswith("/clinic") and "consent" in path for path in paths), paths


class TestPatientMfaIsBlocked:
    """
    UPDATED AT w164, AND TWO OF ITS FOUR ROWS HAVE INVERTED. Rule 3 applied:
    fix the claim, not the code.

    WHAT WAS TRUE AT 60c48d9 AND IS NOW FALSE. Staff MFA was
    FAIL-CLOSED-BROKEN -- `_verify_second_factor` and `enrol_totp` raised
    `ConfigurationError` because every production `DataAccess(...)` passed the
    connection alone -- and `enrol_totp` took the SECRET AS A PARAMETER. Both
    findings were correct and both are now fixed. `DataAccess.__init__`
    derives the cipher from `CLINICAL_TOTP_SECRET_KEYS`, which reaches all
    four construction sites without any of them being edited, and the method
    generates its own secret. The census that asserted the broken state has
    moved to `clinical/tests/test_w164_mfa.py`, where it asserts WHERE the
    cipher comes from instead -- with a row for the key present AND a row for
    the key absent, so it cannot be satisfied by an empty subject.

    WHAT IS STILL TRUE, AND IS STILL A STOP. PATIENT MFA IS ABSENT:
    `patient_logins` has no TOTP column of any kind and `patient_login`
    mentions no second factor. It was not built at w164 and the reason is a
    PRODUCT ruling rather than a technical gap. `enrol_totp` is
    Administrator-gated and presumes an administrator sitting with the account
    holder and a device the organisation controls -- `data_access.py`'s own
    comment above `patient_login` says so. That assumption does not hold for a
    patient at home, and nothing in the repository says how a remote patient
    enrols, what happens when they lose the device, or whether a portal
    account may be locked out of its own results by a second factor. The spec
    is silent: section 23.1 names MFA for "Approver and Administrator" and
    decision 7 reads "MFA for all roles or privileged only -- All roles",
    which is a statement about STAFF ROLES; a patient holds no role. BLOCKED
    -- DECISION REQUIRED, and nothing was guessed.
    """

    def test_the_patient_credential_table_has_no_second_factor_column(self):
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE patient_logins")
        table = schema[start : schema.index(");", start)]
        assert "totp" not in table.lower(), table
        assert "mfa" not in table.lower(), table
        # The twin: the STAFF table does have one, so the scan is not blind.
        users_start = schema.index("CREATE TABLE users")
        assert "totp" in schema[users_start : schema.index(");", users_start)].lower()

    def test_every_production_construction_of_the_data_layer_still_passes_the_connection_alone(self):
        """
        THE CENSUS, INVERTED RATHER THAN DELETED. It used to prove that no
        production site supplies a cipher, and read that as MFA being
        unreachable. Both halves are still measured; only the conclusion
        changed, because the cipher now arrives through the CONSTRUCTOR
        DEFAULT rather than through a keyword.

        THIS IS THE STRONGER PROPERTY, not a weakening. Four sites passing the
        connection alone means there is no fifth site that can forget: a
        worker, a CLI path or a future router that builds a `DataAccess`
        inherits the configured cipher without knowing it exists.
        `test_w164_mfa.py::TestTheKeyComesFromTheEnvironment` is where the
        default itself is proved, in both directions.
        """
        supplied = []
        constructions = 0
        for path in REPO.rglob("*.py"):
            parts = path.parts
            if "tests" in parts or path.name.startswith("test_") or "__pycache__" in parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DataAccess":
                    constructions += 1
                    keywords = {kw.arg for kw in node.keywords}
                    if keywords & {"totp_verifier", "secret_cipher"}:
                        supplied.append((str(path.relative_to(REPO)), node.lineno, sorted(keywords)))
        assert constructions >= 4, f"only {constructions} production constructions found; the census is blind"
        assert supplied == [], supplied

    def test_enrol_totp_generates_the_secret_rather_than_accepting_one(self):
        """
        THE INVERSION OF THE w155 FINDING, kept here beside the census that
        found it. A secret the caller supplies is a secret the caller chose;
        the method now takes no such parameter, so there is no route by which
        a weaker one could arrive.
        """
        tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "enrol_totp")
        names = [a.arg for a in node.args.args]
        assert "secret" not in names, names
        assert "generate_totp_secret" in ast.unparse(node)

    def test_there_is_no_patient_second_factor_code_path_at_all(self):
        """
        Not "the tests do not cover it" -- there is no code. The patient
        authentication method never mentions a second factor.
        """
        tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "patient_login")
        body = ast.unparse(node).lower()
        assert "totp" not in body, body
        assert "second_factor" not in body, body
        # The twin: the STAFF login method does mention both.
        staff = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "login")
        staff_body = ast.unparse(staff).lower()
        assert "totp" in staff_body and "second_factor" in staff_body
