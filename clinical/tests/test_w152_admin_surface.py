"""
clinical/tests/test_w152_admin_surface.py
──────────────────────────────────────────
w152 (WAVE 2) — the administrative HTTP surface, tested as BEHAVIOUR through
the app the service actually serves (`clinical.app`), never a test-local
stand-in and never by reading source alone.

THE FOUR ROUTES UNDER TEST, each one existing domain method:

    POST /admin/users/{user_id}/roles                    assign_role
    POST /admin/users/{user_id}/disabled                 set_user_disabled
    POST /admin/patient-logins/{id}/disabled             set_patient_login_disabled
    GET  /admin/audit                                    read_audit

WHAT THIS FILE DOES NOT PROVE, SAID FIRST SO NOTHING HERE IS MISREAD.
It does NOT prove these routes are reachable by a real deployment. They are
not. Every one of them requires an Administrator or an Auditor session, and
at `e1be7ab` nothing in production can make the first Administrator —
`assign_role` is the only production writer of a human role assignment and it
requires an existing Administrator. The Administrator below is provisioned by
INSERTING THE ROLE ROW DIRECTLY, exactly as `test_identity.py::admin_session`
and `test_w133_staff_revocation.py::_staff_session` already do. That is a
FIXTURE, not a bootstrap, and no bootstrap was invented to make these rows
pass: inventing one would be Wave 3's work done badly and would become a
permanent part of the product. These routes are IMPLEMENTED + UNREACHABLE.

─── THE TWO MEASURED HAZARDS THIS FILE IS SHAPED AROUND ───

1. A REFUSAL ROW THAT ASSERTS ONLY "IT FAILED" IS GREEN AGAINST BROKEN CODE.
   w149's cross-organisation row had to assert the SQLSTATE *and* the
   constraint name, because the pre-fix code failed for the wrong reason.
   So every refusal row here asserts WHY: the exact (status, body) pair, as a
   SET OF SIZE ONE across every case in its class (Standing Rule 8 — an
   oracle can live in either channel, so both are pinned, together). And
   every refusal is paired with a POSITIVE TWIN the broken state fails: the
   same request, from the right principal, succeeding.

2. A MISSING PREDICATE IS INVISIBLE TO EVERY BEHAVIOURAL ROW WHEN
   IDENTIFIERS CANNOT COLLIDE BY ACCIDENT (w148). Two random UUIDs never
   collide, so a handler that dropped the organisation predicate would pass
   every test that only ever names ids from its own tenant.
   `TestTheCrossOrganisationCallIsRefused` therefore CONSTRUCTS THE COLLISION:
   a real user and a real patient credential are planted in organisation B,
   their real ids are named from an organisation A session, and the rows in B
   are then read back and proved UNCHANGED. Without those planted rows this
   whole file is vacuous about tenancy.

─── THE WRONG-FIRST LIST, AND WHICH ROW CATCHES EACH ───

(A) THE ORGANISATION TAKEN FROM THE REQUEST rather than the principal.
    Caught by `TestNoCallerControlledOrganisationEscape` — the query-string
    row (a supplied `org_id` is ignored and the write lands in the session's
    organisation, proved by reading org A's roles and org B's), the body row
    (`extra="forbid"` refuses the field outright), and the structural row
    (no route signature and no request model on this surface mentions an
    organisation at all).
    VACUOUS AGAINST: a handler that reads the organisation from the session
    and then fails to pass it down — the domain method binds it, so that
    version cannot exist here. `TestTheCrossOrganisationCallIsRefused` is
    what covers the predicate itself.

(B) AUTHENTICATION ENFORCED BUT AUTHORISATION NOT.
    Caught by `TestTheWrongRoleIsRefused`: an authenticated Orderer, holding
    a real live session in the right organisation, is refused on all four
    routes, and the write is then proved NOT to have happened.
    VACUOUS AGAINST: a route with no authentication at all —
    `TestTheUnauthenticatedCallIsRefused` is that row.

(C) A 404-VERSUS-403 DISTINCTION THAT REVEALS CROSS-TENANT EXISTENCE.
    Caught by `TestTheRefusalsCarryNoOracle`, which asserts the 404 set is
    size one over {malformed id, id that exists nowhere, REAL id in the other
    organisation} and the 403 set is size one over those same three targets.
    VACUOUS AGAINST: a leak that is not in the refusal — e.g. a 200 that
    returned another tenant's row. `TestTheCrossOrganisationCallIsRefused`
    reads the other tenant's rows back to cover that.

(D) THE AUDIT ROW WRITTEN OUTSIDE THE OPERATION'S TRANSACTION — the
    `@auditable` composition hazard: the decorator COMMITS after each audit
    row, so an operation assembled out of two decorated calls commits the
    first half's audit row when the second half fails.
    Caught by `TestTheHandlersComposeNoDecoratedCalls`: every handler makes
    EXACTLY ONE call on the `DataAccess` instance, read from the AST, with a
    positive twin proving the analyser sees a second call when one exists.
    Paired behaviourally by `TestTheFailedWriteLeavesNothing`.
    VACUOUS AGAINST: a composition inside the DOMAIN layer — not this wave's
    surface, and already pinned by w133's own atomicity rows.

(E) BUSINESS LOGIC REIMPLEMENTED IN THE HANDLER rather than calling the
    domain method.
    Caught by `TestTheHandlersDoNotReimplementTheDomain`: no SQL, no
    `_require_role`, no role vocabulary and no organisation predicate appear
    anywhere in `clinical/admin_endpoints.py`, and each handler is proved to
    call its own domain method by name.
    VACUOUS AGAINST: a handler that calls the right method with the wrong
    arguments — every behavioural row above covers that.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
ADMIN_MODULE = CLINICAL / "admin_endpoints.py"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
STAFF_PASSWORD = "correct horse battery staple"
PATIENT_PASSWORD = "a sufficiently long patient password"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"

# The three refusal bodies, named once. Imported rather than retyped where
# the module owns them, so a change to either constant fails these rows
# rather than silently rewriting what they assert.
from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND  # noqa: E402
from clinical.data_access import GENERIC_AUTH_ERROR  # noqa: E402


def _body(detail):
    """The exact bytes FastAPI serialises `{"detail": ...}` to.

    RAW BYTES, NOT A PARSED DICT, and the reason is Standing Rule 2b read
    literally: the thing that must be indistinguishable is what goes on the
    wire. A parsed dict would also compare equal for two responses whose
    bytes differed in whitespace or key order — and it is not hashable, so a
    refusal SET could not be formed from it at all, which is what Rule 8
    requires. `test_w139_remaining_w132_items.py::THE_REFUSAL` is built the
    same way for the same reason.
    """
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


UNAUTHENTICATED = (401, _body(GENERIC_AUTH_ERROR))
FORBIDDEN = (403, _body(ADMIN_FORBIDDEN))
NOT_FOUND = (404, _body(ADMIN_NOT_FOUND))

# An id that is syntactically a UUID and names nothing anywhere.
NOWHERE = "00000000-0000-0000-0000-000000000000"
# An id that is not a UUID at all.
MALFORMED = "not-a-uuid"


def _pair(response):
    """(status, raw body bytes) — BOTH CHANNELS, as one value.

    Standing Rule 8: asserting the body alone misses the w132 status-channel
    oracle; asserting the status alone misses the w129b body-channel one.
    Collecting the pair lets a class assert the whole refusal set is a set of
    size one, which a per-case equality does not do.
    """
    return (response.status_code, response.content)


# ═══════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════


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
    """The fixture-provisioned role assignment, and it is a FIXTURE.

    Inserted directly because `assign_role` requires an existing
    Administrator and nothing in production makes the first one — the
    deadlock Wave 3 owns. `test_identity.py::admin_session` and
    `test_w133_staff_revocation.py::_staff_session` already break the cycle
    this way; this is the same break, not a new one, and it is deliberately
    NOT a bootstrap that a deployment could use.
    """
    with connection.cursor() as cur:
        for role in roles:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    org_id,
                    role,
                    user_id,
                    dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
                    "fixture" if role == "Approver" else None,
                ),
            )


@pytest.fixture()
def seeded(clinical_dsn):
    """
    TWO ORGANISATIONS, AND THE SECOND ONE IS THE POINT.

    Organisation B is not decoration. It holds a REAL staff user and a REAL
    patient credential whose ids organisation A will name. Two randomly
    generated UUIDs never collide, so without these planted rows a handler
    that dropped its organisation predicate would pass every other row in
    this file.

    Both organisations also get an Auditor, so that "what did the other
    tenant's audit trail record" is a question this file can actually ask.
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
    target_a = dao.create_user(org_a, "target@a.example", STAFF_PASSWORD)

    admin_b = dao.create_user(org_b, "admin@b.example", STAFF_PASSWORD)
    auditor_b = dao.create_user(org_b, "auditor@b.example", STAFF_PASSWORD)
    target_b = dao.create_user(org_b, "target@b.example", STAFF_PASSWORD)
    # w162. ORGANISATION B NEEDS AN ORDERER TOO: the patients this fixture
    # plants are now registered by the ordering clinician (human ruling
    # 2026-09-19), and org A already had one as the wrong-role principal.
    orderer_b = dao.create_user(org_b, "orderer@b.example", STAFF_PASSWORD)

    _grant(connection, admin_a, org_a, ("Administrator",))
    _grant(connection, auditor_a, org_a, ("Auditor",))
    _grant(connection, orderer_a, org_a, ("Orderer",))
    _grant(connection, admin_b, org_b, ("Administrator",))
    _grant(connection, auditor_b, org_b, ("Auditor",))
    _grant(connection, orderer_b, org_b, ("Orderer",))
    connection.commit()

    admin_a_session = dao.login("admin@a.example", org_a, STAFF_PASSWORD)
    auditor_a_session = dao.login("auditor@a.example", org_a, STAFF_PASSWORD)
    orderer_a_session = dao.login("orderer@a.example", org_a, STAFF_PASSWORD)
    admin_b_session = dao.login("admin@b.example", org_b, STAFF_PASSWORD)
    auditor_b_session = dao.login("auditor@b.example", org_b, STAFF_PASSWORD)
    orderer_b_session = dao.login("orderer@b.example", org_b, STAFF_PASSWORD)

    # The patient credentials. A's exists so the route has a same-organisation
    # positive twin; B's exists so the cross-organisation attempt has a REAL
    # row to name and a live credential to prove untouched afterwards.
    # REGISTERED BY THE ORDERER, NOT THE ADMINISTRATOR (w162). These two calls
    # used to take the Administrator sessions, which worked because
    # `create_patient` enforced no role at all. The gate is not weakened and
    # the method is not bypassed; every claim in this file is about the
    # ADMINISTRATIVE routes, and only the construction of their targets moved.
    patient_a = dao.create_patient(orderer_a_session, "Alice Anderson", dt.date(1980, 1, 1), "F")
    patient_b = dao.create_patient(orderer_b_session, "Bianca Bell", dt.date(1975, 6, 15), "F")
    login_a = dao.enrol_patient_login(admin_a_session, patient_a, "alice@portal.example", PATIENT_PASSWORD)
    login_b = dao.enrol_patient_login(admin_b_session, patient_b, "bianca@portal.example", PATIENT_PASSWORD)

    # A live patient session in A, so the disable route's effect on live
    # sessions is observable rather than merely claimed.
    patient_a_session = dao.patient_login("alice@portal.example", org_a, PATIENT_PASSWORD)

    # A live staff session belonging to the disable route's TARGET, for the
    # same reason.
    target_a_session = dao.login("target@a.example", org_a, STAFF_PASSWORD)

    connection.commit()
    connection.close()

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_a": admin_a,
        "auditor_a": auditor_a,
        "orderer_a": orderer_a,
        "target_a": target_a,
        "admin_b": admin_b,
        "auditor_b": auditor_b,
        "target_b": target_b,
        "admin_a_sid": str(admin_a_session.session_id),
        "auditor_a_sid": str(auditor_a_session.session_id),
        "orderer_a_sid": str(orderer_a_session.session_id),
        "admin_b_sid": str(admin_b_session.session_id),
        "auditor_b_sid": str(auditor_b_session.session_id),
        "target_a_sid": str(target_a_session.session_id),
        "patient_a": patient_a,
        "patient_b": patient_b,
        "login_a": login_a,
        "login_b": login_b,
        "patient_a_sid": str(patient_a_session.session_id),
    }


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app)


@pytest.fixture()
def reader(clinical_dsn):
    """
    A SECOND, INDEPENDENT `DataAccess` FOR READING BACK WHAT THE ROUTES DID.

    STANDING RULE 2a: every assertion about state goes through the DOMAIN
    METHOD (`get_user`, `get_user_roles`, `patient_login`, `session_valid`),
    never through `connection.execute` with hand-written SQL. A test that
    builds its expectation from raw SQL passes while the method that is
    supposed to read it is broken — the Phase 5d cannot-fail shape. It is
    doubly wrong here, because a hand-written SELECT could omit the very
    `org_id` predicate this file exists to prove.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
    yield dao
    connection.close()


def _roles_of(reader, session_id, user_id):
    """The active roles of a user, THROUGH THE DOMAIN READER and through a
    session re-derived exactly as `_get_session` derives it."""
    session = reader.session_valid(session_id)
    return sorted(a.role for a in reader.get_user_roles(session, user_id))


# ═══════════════════════════════════════════════════════════════════════════
# THE SURFACE EXISTS, AND IT IS THE FIRST ADMINISTRATIVE ONE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheSurfaceIsMounted:
    def test_the_four_administrative_routes_are_served(self):
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        assert {
            "/admin/users/{user_id}/roles",
            "/admin/users/{user_id}/disabled",
            "/admin/patient-logins/{patient_login_id}/disabled",
            "/admin/audit",
        } <= paths, sorted(paths)

    def test_before_this_wave_the_served_surface_had_no_administrative_route(self):
        """
        THE NON-VACUITY TWIN FOR THE ROW ABOVE, and the measurement w150
        made. The claim "zero of seventeen routes were administrative" is a
        census, so it gets counted rather than recited: every path that is
        NOT under /admin is checked to be one of the four already-known
        non-administrative families. A fifth family appearing here means the
        surface grew somewhere this wave did not look.

        w157 (WAVE 7) ADDED THE FIFTH FAMILY AND THIS GUARD IS WHAT MADE IT
        SAY SO. `/lab/` is the laboratory bench surface — `POST
        /lab/orders/{order_id}/samples` and `POST /lab/samples/{sample_id}/qc`,
        the Sample and QC stages of the operational chain, `Lab technician`-
        gated inside the domain method. It is listed here DELIBERATELY rather
        than by widening the predicate, because the whole value of this row is
        that an undeclared family fails it: the guard fired on the wave that
        added one, which is the guard working, and the fix is a declaration
        rather than a relaxation.

        w162 ADDED THE SIXTH FAMILY, AND THE GUARD FIRED AGAIN — which is the
        guard working a second time. `/clinic/` is the ordering clinician's
        surface: `POST /clinic/patients`, `POST
        /clinic/patients/{patient_id}/consents` and `POST
        /clinic/consents/{consent_id}/withdrawal`, all `Orderer`-gated inside
        the domain method (human ruling 2026-09-19: the ordering clinician
        owns patient registration and consent). It is declared here rather
        than absorbed by a looser predicate, for the same reason `/lab/` was.

        AND IT IS NOT ADMINISTRATIVE, which is the point of the family being
        separate. The ruling forbids making Administrator registration
        mandatory, so filing these under `/admin/` would have encoded in the
        URL the reading the ruling rejects.

        The historical claim the row exists to protect is UNCHANGED and is
        still true: at w150 zero of the seventeen served routes were
        administrative. Neither `/lab/` nor `/clinic/` is administrative
        either.

        STAGE 3 ADDED THE SEVENTH FAMILY, AND THE GUARD FIRED A THIRD TIME --
        the guard working again. `/review/` is the review surface: the
        Interpreter's claims and submission and the Approver's approval and
        release, every gate inside the domain method. It is declared here
        rather than absorbed by a looser predicate, and it is not
        administrative either.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        non_admin = {path for path, _ in served_routes(app) if not path.startswith("/admin/")}
        assert non_admin and all(
            path.startswith(
                ("/auth/", "/portal/", "/exceptions/", "/lab/", "/clinic/", "/review/", "/healthz", "/readyz")
            )
            for path in non_admin
        ), sorted(non_admin)

    def test_every_administrative_route_requires_the_staff_session_dependency(self):
        """
        ENUMERATED FROM THE ROUTER, not read from the source. A route added
        later without `Depends(_get_session)` is an unauthenticated
        administrative route, and nothing else in this repository would
        notice it.
        """
        from clinical.admin_endpoints import router
        from clinical.endpoints import _get_session

        for route in router.routes:
            dependencies = [d.call for d in getattr(route, "dependant").dependencies]
            assert _get_session in dependencies, (route.path, dependencies)

    def test_the_twin_the_dependency_check_can_see_its_absence(self):
        """Standing Rule 4: without this, the row above is satisfied by a
        check that finds nothing on any route."""
        from fastapi import APIRouter

        from clinical.endpoints import _get_session

        probe = APIRouter()

        @probe.get("/unguarded")
        async def unguarded() -> dict:
            return {}

        for route in probe.routes:
            dependencies = [d.call for d in getattr(route, "dependant").dependencies]
            assert _get_session not in dependencies, "the check cannot distinguish a guarded route from an open one"


# ═══════════════════════════════════════════════════════════════════════════
# THE AUTHORISED CALL SUCCEEDS — the positive twin every refusal row needs
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuthorisedCallSucceeds:
    def test_an_administrator_can_assign_a_role_and_the_role_is_really_granted(self, client, seeded, reader):
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Orderer"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["assignment_id"]

        # Rule 2a: through the domain reader, never a hand-written SELECT.
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Orderer"]

    def test_the_approver_basis_rule_is_the_domain_methods_and_is_not_copied(self, client, seeded, reader):
        """
        Approver without a basis is refused (ISO 15189 7.4.1.5 c), and with a
        basis it is granted. BOTH halves, because the refusal alone is
        satisfied by a handler that refuses everything.
        """
        without = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Approver"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert without.status_code == 400, without.text
        assert "basis" in without.json()["detail"]
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == []

        with_basis = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Approver", "basis": "FRCPath, 2019"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert with_basis.status_code == 201, with_basis.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver"]

    def test_the_system_role_is_still_ungrantable_through_the_new_surface(self, client, seeded, reader):
        """
        w141 made `'System'` unassignable to a person. A new HTTP surface is
        exactly how such a rule quietly stops applying — this proves the
        route inherits it rather than re-declaring it.
        """
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "System"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 400, response.text
        assert "System" in response.json()["detail"]
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == []

    def test_an_administrator_can_disable_a_user_and_the_users_live_session_ends(self, client, seeded, reader):
        from clinical.data_access import AuthenticationError

        # The target's session is live BEFORE the call — otherwise "it is
        # dead afterwards" is satisfied by it never having worked.
        assert reader.session_valid(seeded["target_a_sid"]) is not None

        response = client.post(
            f"/admin/users/{seeded['target_a']}/disabled",
            json={"disabled": True},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 204, response.text
        assert response.content == b""

        admin_session = reader.session_valid(seeded["admin_a_sid"])
        assert reader.get_user(admin_session, seeded["target_a"]).disabled is True
        with pytest.raises(AuthenticationError):
            reader.session_valid(seeded["target_a_sid"])

    def test_the_same_route_re_enables(self, client, seeded, reader):
        client.post(
            f"/admin/users/{seeded['target_a']}/disabled",
            json={"disabled": True},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        response = client.post(
            f"/admin/users/{seeded['target_a']}/disabled",
            json={"disabled": False},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 204, response.text
        admin_session = reader.session_valid(seeded["admin_a_sid"])
        assert reader.get_user(admin_session, seeded["target_a"]).disabled is False

    def test_an_administrator_can_revoke_a_patient_credential_and_the_patient_cannot_log_in(
        self, client, seeded, reader
    ):
        from clinical.data_access import AuthenticationError

        # It works BEFORE. Without this the row below is satisfied by a
        # credential that never authenticated.
        assert reader.patient_login("alice@portal.example", seeded["org_a"], PATIENT_PASSWORD) is not None

        response = client.post(
            f"/admin/patient-logins/{seeded['login_a']}/disabled",
            json={"disabled": True},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 204, response.text

        with pytest.raises(AuthenticationError):
            reader.patient_login("alice@portal.example", seeded["org_a"], PATIENT_PASSWORD)
        with pytest.raises(AuthenticationError):
            reader.patient_session_valid(seeded["patient_a_sid"])

    def test_an_auditor_can_read_the_audit_trail(self, client, seeded):
        response = client.get("/admin/audit", cookies={STAFF_COOKIE: seeded["auditor_a_sid"]})
        assert response.status_code == 200, response.text
        rows = response.json()
        assert rows, "the seeding alone writes audit rows; an empty trail means the read is not reading"
        assert {row["org_id"] for row in rows} == {str(seeded["org_a"])}, (
            "read_audit binds org_id from the session; a second organisation here is a tenant leak"
        )

    def test_the_action_filter_narrows_and_is_not_an_identifier(self, client, seeded):
        client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Orderer"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        response = client.get(
            "/admin/audit",
            params={"action": "role_assigned"},
            cookies={STAFF_COOKIE: seeded["auditor_a_sid"]},
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert rows, rows
        assert {row["action"] for row in rows} == {"role_assigned"}


# ═══════════════════════════════════════════════════════════════════════════
# (B's twin) AUTHENTICATION — every way of not being authenticated, as ONE
# refusal
# ═══════════════════════════════════════════════════════════════════════════


class TestTheUnauthenticatedCallIsRefused:
    def _every_route(self, client, seeded, cookies):
        return [
            client.post(f"/admin/users/{seeded['target_a']}/roles", json={"role": "Orderer"}, cookies=cookies),
            client.post(f"/admin/users/{seeded['target_a']}/disabled", json={"disabled": True}, cookies=cookies),
            client.post(
                f"/admin/patient-logins/{seeded['login_a']}/disabled", json={"disabled": True}, cookies=cookies
            ),
            client.get("/admin/audit", cookies=cookies),
        ]

    def test_no_credential_a_malformed_one_and_an_unknown_one_are_one_refusal(self, client, seeded):
        answers = set()
        for cookies in (
            {},
            {STAFF_COOKIE: "not-a-uuid"},
            {STAFF_COOKIE: NOWHERE},
        ):
            for response in self._every_route(client, seeded, cookies):
                answers.add(_pair(response))
        assert answers == {UNAUTHENTICATED}, answers

    def test_a_patient_session_id_is_not_a_staff_credential(self, client, seeded):
        """
        THE PRINCIPAL-CONFUSION ROW. A patient session id is a real, live,
        valid credential — for the other principal. Presented to an
        administrative route it must be indistinguishable from nonsense.
        """
        answers = {
            _pair(response) for response in self._every_route(client, seeded, {STAFF_COOKIE: seeded["patient_a_sid"]})
        }
        assert answers == {UNAUTHENTICATED}, answers

    def test_a_terminated_staff_session_is_refused(self, client, seeded, reader):
        session = reader.session_valid(seeded["admin_a_sid"])
        reader.terminate_my_session(session)

        answers = {
            _pair(response) for response in self._every_route(client, seeded, {STAFF_COOKIE: seeded["admin_a_sid"]})
        }
        assert answers == {UNAUTHENTICATED}, answers

    def test_the_bearer_header_authenticates_too_so_the_rows_above_are_not_cookie_only(self, client, seeded):
        """
        THE POSITIVE TWIN FOR THIS WHOLE CLASS. Without it, every row above
        is satisfied by a surface that refuses everyone.
        """
        response = client.get("/admin/audit", headers={"Authorization": f"Bearer {seeded['auditor_a_sid']}"})
        assert response.status_code == 200, response.text


# ═══════════════════════════════════════════════════════════════════════════
# (B) AUTHENTICATED AND NOT AUTHORISED
# ═══════════════════════════════════════════════════════════════════════════


class TestTheWrongRoleIsRefused:
    def test_an_orderer_is_refused_on_every_administrative_route(self, client, seeded, reader):
        cookies = {STAFF_COOKIE: seeded["orderer_a_sid"]}
        answers = {
            _pair(client.post(f"/admin/users/{seeded['target_a']}/roles", json={"role": "Orderer"}, cookies=cookies)),
            _pair(client.post(f"/admin/users/{seeded['target_a']}/disabled", json={"disabled": True}, cookies=cookies)),
            _pair(
                client.post(
                    f"/admin/patient-logins/{seeded['login_a']}/disabled", json={"disabled": True}, cookies=cookies
                )
            ),
            _pair(client.get("/admin/audit", cookies=cookies)),
        }
        assert answers == {FORBIDDEN}, answers

        # AND NOTHING HAPPENED. A refusal row that asserts only the status is
        # green against a handler that refuses AFTER writing.
        admin_session = reader.session_valid(seeded["admin_a_sid"])
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == []
        assert reader.get_user(admin_session, seeded["target_a"]).disabled is False
        assert reader.patient_login("alice@portal.example", seeded["org_a"], PATIENT_PASSWORD) is not None

    def test_an_administrator_is_not_an_auditor(self, client, seeded):
        """
        THE GATE IS PER-METHOD, NOT "IS THIS PERSON AN ADMIN". `read_audit`
        requires Auditor; an Administrator who holds no Auditor role is
        refused by the domain method, and this route does not widen it.
        """
        response = client.get("/admin/audit", cookies={STAFF_COOKIE: seeded["admin_a_sid"]})
        assert _pair(response) == FORBIDDEN, response.text

    def test_an_auditor_is_not_an_administrator(self, client, seeded, reader):
        cookies = {STAFF_COOKIE: seeded["auditor_a_sid"]}
        assert (
            _pair(client.post(f"/admin/users/{seeded['target_a']}/roles", json={"role": "Orderer"}, cookies=cookies))
            == FORBIDDEN
        )
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == []


# ═══════════════════════════════════════════════════════════════════════════
# (C) THE CROSS-ORGANISATION CALL — PROVED BY ATTEMPTING IT, AGAINST A
# CONSTRUCTED COLLISION
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCrossOrganisationCallIsRefused:
    """
    Every row here names a REAL id belonging to organisation B from an
    organisation A Administrator session. w148's lesson: identifiers that
    cannot collide by accident make a missing predicate invisible, so the
    collision is constructed and the other tenant's rows are read back.
    """

    def test_a_real_user_in_the_other_organisation_cannot_be_given_a_role(self, client, seeded, reader):
        response = client.post(
            f"/admin/users/{seeded['target_b']}/roles",
            json={"role": "Administrator"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert _pair(response) == NOT_FOUND, response.text

        # THE ROW IN B IS UNCHANGED, read with B's own Administrator session.
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == []
        # ...and it was not created in A either, which is the other way a
        # missing predicate can express itself.
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_b"]) == []

    def test_a_real_user_in_the_other_organisation_cannot_be_disabled(self, client, seeded, reader):
        response = client.post(
            f"/admin/users/{seeded['target_b']}/disabled",
            json={"disabled": True},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert _pair(response) == NOT_FOUND, response.text

        admin_b_session = reader.session_valid(seeded["admin_b_sid"])
        assert reader.get_user(admin_b_session, seeded["target_b"]).disabled is False

    def test_a_real_patient_credential_in_the_other_organisation_cannot_be_revoked(self, client, seeded, reader):
        response = client.post(
            f"/admin/patient-logins/{seeded['login_b']}/disabled",
            json={"disabled": True},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert _pair(response) == NOT_FOUND, response.text

        # BEHAVIOURAL, not a flag read: B's patient can still authenticate.
        assert reader.patient_login("bianca@portal.example", seeded["org_b"], PATIENT_PASSWORD) is not None

    def test_the_positive_twin_the_same_request_inside_the_right_organisation_succeeds(self, client, seeded, reader):
        """
        Standing Rule 4. Without this, all three rows above are satisfied by
        a surface that refuses every target id in existence.
        """
        assert (
            client.post(
                f"/admin/users/{seeded['target_b']}/roles",
                json={"role": "Administrator"},
                cookies={STAFF_COOKIE: seeded["admin_b_sid"]},
            ).status_code
            == 201
        )
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == ["Administrator"]

    def test_an_auditor_cannot_see_the_other_organisations_trail_and_the_row_is_planted(self, client, seeded, reader):
        """
        THE AUDIT HALF OF THE SAME COLLISION. A distinctive event is planted
        in organisation B through the route itself, then looked for in
        organisation A's trail BY NAME. An absence assertion over a trail
        that never had the row is vacuous; this one plants it first.
        """
        planted = client.post(
            f"/admin/users/{seeded['target_b']}/roles",
            json={"role": "Approver", "basis": "planted-in-org-b"},
            cookies={STAFF_COOKIE: seeded["admin_b_sid"]},
        )
        assert planted.status_code == 201, planted.text

        in_b = client.get(
            "/admin/audit", params={"action": "role_assigned"}, cookies={STAFF_COOKIE: seeded["auditor_b_sid"]}
        ).json()
        assert any("planted-in-org-b" in str(row["details"]) for row in in_b), in_b

        in_a = client.get(
            "/admin/audit", params={"action": "role_assigned"}, cookies={STAFF_COOKIE: seeded["auditor_a_sid"]}
        ).json()
        assert not any("planted-in-org-b" in str(row["details"]) for row in in_a), in_a
        assert {row["org_id"] for row in in_a} <= {str(seeded["org_a"])}, in_a


# ═══════════════════════════════════════════════════════════════════════════
# (C) NO INFORMATION ORACLE — the refusal sets, each of size one
# ═══════════════════════════════════════════════════════════════════════════


class TestTheRefusalsCarryNoOracle:
    def _targets(self, seeded, kind):
        """The three target shapes a refusal must not tell apart: an id that
        is not a UUID, a UUID that names nothing anywhere, and a REAL id
        belonging to the other organisation."""
        if kind == "user":
            return (MALFORMED, NOWHERE, str(seeded["target_b"]))
        return (MALFORMED, NOWHERE, str(seeded["login_b"]))

    def test_the_404_is_one_object_over_malformed_absent_and_other_tenant(self, client, seeded):
        """
        THE ORACLE THIS CLOSES, STATED: if "no such user" differed from "that
        user is in another organisation", an Administrator could enumerate
        another tenant's user ids from inside their own. The three cases are
        asserted as a SET, not pairwise — a pairwise equality passes against a
        leak in a case nobody listed.
        """
        answers = set()
        for target in self._targets(seeded, "user"):
            answers.add(
                _pair(
                    client.post(
                        f"/admin/users/{target}/roles",
                        json={"role": "Orderer"},
                        cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                    )
                )
            )
            answers.add(
                _pair(
                    client.post(
                        f"/admin/users/{target}/disabled",
                        json={"disabled": True},
                        cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                    )
                )
            )
        for target in self._targets(seeded, "login"):
            answers.add(
                _pair(
                    client.post(
                        f"/admin/patient-logins/{target}/disabled",
                        json={"disabled": True},
                        cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                    )
                )
            )
        assert answers == {NOT_FOUND}, answers

    def test_the_403_does_not_vary_with_the_target_at_all(self, client, seeded):
        """
        WHY THE 403/404 SPLIT IS NOT ITSELF AN ORACLE. The role gate runs
        FIRST, inside the domain method, so an unauthorised caller gets the
        identical refusal for a real same-organisation target, an invented
        one, and a real one in ANOTHER organisation. If it did not, the 403
        would be the enumeration oracle instead of the 404.

        AND IT IS NOT A HYPOTHETICAL ROW. `MALFORMED` is in this list because
        the first version of `admin_endpoints._target` RAISED the 404 for an
        unparseable identifier — in the handler body, which runs before the
        domain method and therefore before the role gate. Against that
        version this row observes {403, 404} and fails; every other row in
        this file stays green. It is the only row that sees it.
        """
        answers = set()
        for target in (str(seeded["target_a"]), NOWHERE, str(seeded["target_b"]), MALFORMED):
            answers.add(
                _pair(
                    client.post(
                        f"/admin/users/{target}/roles",
                        json={"role": "Orderer"},
                        cookies={STAFF_COOKIE: seeded["orderer_a_sid"]},
                    )
                )
            )
        assert answers == {FORBIDDEN}, answers

    def test_the_unauthenticated_refusal_is_the_same_whatever_is_named(self, client, seeded):
        answers = set()
        for target in (str(seeded["target_a"]), str(seeded["target_b"]), NOWHERE, MALFORMED):
            answers.add(_pair(client.post(f"/admin/users/{target}/roles", json={"role": "Orderer"})))
        assert answers == {UNAUTHENTICATED}, answers

    def test_the_three_refusals_are_three_distinct_objects_and_no_more(self, client, seeded):
        """
        THE THREE PRINCIPAL-AND-EXISTENCE REFUSALS, asserted as three
        distinct objects over the SAME request differing only in who sent it.
        A fourth shape appearing here would be a new channel nobody reviewed.

        SAID PRECISELY, because "the surface's whole refusal vocabulary"
        would be false and this file does not make claims it has not counted:
        the surface also answers 400 (an unknown or unassignable role — the
        closed seven-string vocabulary, raised strictly AFTER the
        Administrator gate) and 422 (a forbidden extra body field, or a
        `limit` outside 1..1000). Neither is an identifier channel: neither
        varies with the existence of any row, in this tenant or another.
        """
        assert {UNAUTHENTICATED, FORBIDDEN, NOT_FOUND} == {
            _pair(client.post(f"/admin/users/{NOWHERE}/roles", json={"role": "Orderer"})),
            _pair(
                client.post(
                    f"/admin/users/{NOWHERE}/roles",
                    json={"role": "Orderer"},
                    cookies={STAFF_COOKIE: seeded["orderer_a_sid"]},
                )
            ),
            _pair(
                client.post(
                    f"/admin/users/{NOWHERE}/roles",
                    json={"role": "Orderer"},
                    cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                )
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════
# (A) NO CALLER-CONTROLLED ORGANISATION ESCAPE
# ═══════════════════════════════════════════════════════════════════════════


class TestNoCallerControlledOrganisationEscape:
    def test_an_organisation_in_the_query_string_is_ignored_and_the_write_lands_in_the_session_s_org(
        self, client, seeded, reader
    ):
        """
        THE ESCAPE ATTEMPT, MADE. A supplied `org_id` naming organisation B
        is attached to a request that otherwise succeeds. It must change
        NOTHING: the role must appear in A, and B's target must be untouched.
        """
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            params={"org_id": str(seeded["org_b"]), "organisation": str(seeded["org_b"])},
            json={"role": "Orderer"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 201, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Orderer"]
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == []

    def test_an_organisation_in_the_body_is_refused_rather_than_ignored(self, client, seeded, reader):
        """
        `extra="forbid"`. Ignoring it would be safe and SILENT; refusing it
        is safe and LOUD, so the day someone adds the field the 422 is what
        tells them the surface never accepted one.
        """
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Orderer", "org_id": str(seeded["org_b"])},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 422, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == []

    def test_an_actor_in_the_body_is_refused_too(self, client, seeded):
        """
        The audit actor is `session.user_id` and can never be claimed. This
        is `ResolutionRequest`'s rule, applied to the new surface.
        """
        response = client.post(
            f"/admin/users/{seeded['target_a']}/disabled",
            json={"disabled": True, "assigned_by": str(seeded["admin_b"])},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 422, response.text

    def test_structurally_no_route_signature_or_request_model_names_an_organisation(self):
        """
        DEFENCE BY ABSENT PARAMETER, checked as an absence in the syntax tree
        rather than trusted. The behavioural rows above cover the parameters
        that exist; this covers the one that must not come to exist.

        REQUEST models only. `AuditEntry` is a RESPONSE model and carries an
        `org_id` on purpose — it can only ever be the caller's own, and
        having it in the shape is what lets
        `test_an_auditor_can_read_the_audit_trail` assert that invariant on
        the response instead of on the SQL. An inbound organisation is the
        thing that must not exist, so the scan is scoped to the models a
        caller can send and to the handler signatures, and both scopes are
        proved non-empty.
        """
        tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))

        handlers = []
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                handlers.append(node.name)
                for arg in node.args.args + node.args.kwonlyargs:
                    if "org" in arg.arg.lower():
                        offenders.append((node.name, arg.arg))

        request_models = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # A request model is one that declares `extra="forbid"`; that is
            # also the property being relied on, so identifying them by it
            # means a model that loses the setting leaves this scan rather
            # than silently passing it.
            declares_forbid = any(
                "forbid" in ast.unparse(stmt) for stmt in node.body if isinstance(stmt, (ast.Assign, ast.AnnAssign))
            )
            if not declares_forbid:
                continue
            request_models.append(node.name)
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if "org" in stmt.target.id.lower():
                        offenders.append((node.name, stmt.target.id))

        assert {"assign_role", "set_user_disabled", "set_patient_login_disabled", "read_audit"} <= set(handlers), (
            handlers
        )
        # w155 added `PatientLoginEnrolmentRequest`. It is in this list, and
        # therefore inside the "no inbound organisation" scan, precisely
        # because it is the newest place an `org_id` could be added by
        # somebody who had not read this file.
        # w156 added `RetractionRequest`, and it is the sharpest case this
        # scan has had: it is the first request model on this surface with a
        # second IDENTIFIER field (`replacement_report_id`, optional by ruling
        # E4), so it is the newest place both an `org_id` and a
        # caller-supplied actor could be added by somebody who had not read
        # this file.
        assert sorted(request_models) == [
            "DisabledRequest",
            "PatientLoginEnrolmentRequest",
            "RetractionRequest",
            "RoleAssignmentRequest",
            # Stage 2. `TestCreateRequest` declares `extra="forbid"` like the
            # rest and carries no organisation, approver or actor field, so it
            # sits inside the same "no inbound organisation" scan.
            "TestCreateRequest",
        ], request_models
        assert offenders == [], offenders

    def test_the_twin_the_scan_finds_an_organisation_parameter_when_one_exists(self, tmp_path):
        """Standing Rule 4: the absence row above is vacuous if the matcher
        cannot see the thing it forbids."""
        probe = tmp_path / "probe.py"
        probe.write_text("async def handler(org_id: str, user_id: str) -> None:\n    return None\n", encoding="utf-8")
        tree = ast.parse(probe.read_text(encoding="utf-8"))
        found = [
            arg.arg
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for arg in node.args.args
            if "org" in arg.arg.lower()
        ]
        assert found == ["org_id"], found


# ═══════════════════════════════════════════════════════════════════════════
# AUDIT ATTRIBUTION
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuditRowNamesTheRightActorOrganisationAndTarget:
    def test_the_role_assignment_is_attributed_to_the_acting_administrator(self, client, seeded):
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Orderer"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 201, response.text

        rows = client.get(
            "/admin/audit", params={"action": "role_assigned"}, cookies={STAFF_COOKIE: seeded["auditor_a_sid"]}
        ).json()
        matching = [row for row in rows if row["details"].get("target_user_id") == str(seeded["target_a"])]
        assert len(matching) == 1, rows
        entry = matching[0]
        assert entry["user_id"] == str(seeded["admin_a"]), entry
        assert entry["org_id"] == str(seeded["org_a"]), entry
        assert entry["details"]["role"] == "Orderer", entry

    def test_the_disable_and_the_enable_are_different_events(self, client, seeded):
        for disabled in (True, False):
            assert (
                client.post(
                    f"/admin/users/{seeded['target_a']}/disabled",
                    json={"disabled": disabled},
                    cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                ).status_code
                == 204
            )

        rows = client.get("/admin/audit", cookies={STAFF_COOKIE: seeded["auditor_a_sid"]}).json()
        actions = [row["action"] for row in rows if row["action"] in ("user_disabled", "user_enabled")]
        assert actions == ["user_disabled", "user_enabled"], actions
        for row in rows:
            if row["action"] in ("user_disabled", "user_enabled"):
                assert row["user_id"] == str(seeded["admin_a"]), row
                assert row["details"]["target_user_id"] == str(seeded["target_a"]), row

    def test_the_credential_revocation_names_the_credential_not_the_patient(self, client, seeded):
        assert (
            client.post(
                f"/admin/patient-logins/{seeded['login_a']}/disabled",
                json={"disabled": True},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 204
        )
        rows = client.get(
            "/admin/audit",
            params={"action": "patient_login_disabled"},
            cookies={STAFF_COOKIE: seeded["auditor_a_sid"]},
        ).json()
        assert len(rows) == 1, rows
        assert rows[0]["user_id"] == str(seeded["admin_a"]), rows[0]
        assert rows[0]["details"]["target_patient_login_id"] == str(seeded["login_a"]), rows[0]

    def test_a_refused_attempt_is_recorded_against_the_caller_and_not_against_the_target_tenant(self, client, seeded):
        """
        `@auditable` writes a row on the failing paths too — `outcome='denied'`
        for an `AuthorizationError`, `outcome='error'` for anything else, and a
        cross-organisation target raises `NotFoundError`, so this attempt takes
        the second branch. Neither outcome is in the columns `read_audit`
        selects, so what is asserted here is what the route actually returns:
        the action, the actor and the ORGANISATION the row was filed under.

        It must land in the CALLER'S organisation. An attempt recorded in the
        tenant that was probed would tell that tenant an id of theirs was
        guessed, and would put a stranger's action in their trail.
        """
        assert (
            client.post(
                f"/admin/users/{seeded['target_b']}/roles",
                json={"role": "Administrator"},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 404
        )

        in_a = client.get(
            "/admin/audit", params={"action": "role_assigned"}, cookies={STAFF_COOKIE: seeded["auditor_a_sid"]}
        ).json()
        assert any(row["details"].get("target_user_id") == str(seeded["target_b"]) for row in in_a), in_a
        assert all(row["org_id"] == str(seeded["org_a"]) for row in in_a), in_a

        in_b = client.get(
            "/admin/audit", params={"action": "role_assigned"}, cookies={STAFF_COOKIE: seeded["auditor_b_sid"]}
        ).json()
        assert in_b == [], in_b


# ═══════════════════════════════════════════════════════════════════════════
# (D) THE @auditable COMPOSITION HAZARD
# ═══════════════════════════════════════════════════════════════════════════


def _dao_calls_per_handler(source):
    """Every `dao.<method>(...)` call, grouped by the enclosing function.

    AST, not grep: this module's own docstring names `assign_role`,
    `set_user_disabled`, `read_audit` and `create_amendment` in prose, and a
    text search would count every one of them as a call. That error is
    exactly why w150 could not use grep for reachability.
    """
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [
                call.func.attr
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "dao"
            ]
            if calls:
                found[node.name] = calls
    return found


class TestTheHandlersComposeNoDecoratedCalls:
    def test_each_handler_calls_exactly_one_domain_method(self):
        """
        THE HAZARD, STATED: `@auditable` COMMITS after each audit row it
        writes. A handler that called two decorated methods — "check it, then
        do it", or "do it, then read it back to return it" — would commit the
        first one's audit row when the second failed, and the audit would
        record something that did not happen. w133 wrote
        `set_patient_login_disabled` as one body for exactly this reason; a
        handler is free to undo that, and nothing else would notice.
        """
        per_handler = _dao_calls_per_handler(ADMIN_MODULE.read_text(encoding="utf-8"))
        assert per_handler, "no dao calls found at all -- this guard is reading the wrong file"
        assert set(per_handler) == {
            "assign_role",
            # w154 (WAVE 4). The fifth handler, added because role revocation
            # had no production writer at all. This set is an exact census of
            # the surface on purpose -- a handler appearing in it without
            # being named here is a route nobody enumerated -- so growing the
            # surface means growing the census, which is what this line is.
            "revoke_role",
            "set_user_disabled",
            # w155 (WAVE 5). Patient credential ISSUANCE, the half w152
            # stopped on spec §16 and the human's Wave 5 dispatch then
            # instructed. One decorated call, like every other handler here:
            # a version that read the patient back to echo its name would
            # commit the enrolment's audit row and then fail.
            "enrol_patient_login",
            "set_patient_login_disabled",
            "read_audit",
            # w156 (WAVE 6). Report retraction — the verb did not exist at
            # 7bb6f8c and was written this wave, so this is a handler for a
            # capability that was ABSENT rather than implemented-unreachable.
            # It is APPROVER-gated, which makes it the second route on this
            # surface whose role is not Administrator (`read_audit` is
            # Auditor): the /admin prefix names the surface, not the role.
            # One decorated call, like every other handler here.
            "retract_report",
            # w164. TOTP enrolment, and it is the sharpest case this row has
            # had. The obvious wrong version is a handler that enrols and then
            # reads the user back to return `totp_enrolled_at` -- two decorated
            # calls, so `@auditable` would commit the ENROLMENT'S audit row and
            # then fail, leaving an audit trail that records a second factor
            # nobody holds. It returns what the single domain call returns and
            # nothing else.
            "enrol_totp",
            # Stage 2. The test catalogue's two administrative verbs. One
            # decorated call each, same-named as the domain method.
            "create_test",
            "retire_test",
            # Milestone 2, Phase A. The staff directory: three READ-ONLY handlers, each a single decorated call
            # with the same name as the domain method it wraps. They write nothing, so the hazard this row
            # guards (a committed audit row for something that did not happen) cannot arise through them.
            "list_staff_users",
            "read_staff_user",
            "read_own_organisation",
        }, per_handler
        for handler, calls in per_handler.items():
            assert len(calls) == 1, (handler, calls)
            assert calls[0] == handler, (
                f"{handler} calls dao.{calls[0]} -- the handler and the domain method it wraps "
                "should be the same operation, not two"
            )

    def test_the_twin_the_analyser_sees_a_second_call_when_one_exists(self, tmp_path):
        probe = tmp_path / "probe.py"
        probe.write_text(
            "async def handler(dao):\n    dao.set_user_disabled(s, t, True)\n    return dao.get_user(s, t)\n",
            encoding="utf-8",
        )
        found = _dao_calls_per_handler(probe.read_text(encoding="utf-8"))
        assert found == {"handler": ["set_user_disabled", "get_user"]}, found

    def test_the_twin_prose_naming_a_method_is_not_counted_as_a_call(self, tmp_path):
        probe = tmp_path / "probe.py"
        probe.write_text(
            "async def handler(dao):\n"
            '    """Calls dao.set_user_disabled and then dao.get_user."""\n'
            "    return dao.read_audit(s)\n",
            encoding="utf-8",
        )
        found = _dao_calls_per_handler(probe.read_text(encoding="utf-8"))
        assert found == {"handler": ["read_audit"]}, found


class TestTheFailedWriteLeavesNothing:
    def test_a_refused_role_assignment_creates_no_assignment_in_either_organisation(self, client, seeded, reader):
        """The behavioural half of (D). Whatever the audit path did, no role
        row exists anywhere afterwards."""
        assert (
            client.post(
                f"/admin/users/{seeded['target_b']}/roles",
                json={"role": "Administrator"},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 404
        )
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_b"]) == []
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == []

    def test_a_refused_credential_revocation_leaves_the_credential_live(self, client, seeded, reader):
        assert (
            client.post(
                f"/admin/patient-logins/{seeded['login_b']}/disabled",
                json={"disabled": True},
                cookies={STAFF_COOKIE: seeded["orderer_a_sid"]},
            ).status_code
            == 403
        )
        assert reader.patient_login("bianca@portal.example", seeded["org_b"], PATIENT_PASSWORD) is not None


# ═══════════════════════════════════════════════════════════════════════════
# (E) NO BUSINESS LOGIC IN THE ROUTER
# ═══════════════════════════════════════════════════════════════════════════


def _non_docstring_strings(source):
    """Every string CONSTANT in the module that is not a docstring.

    Docstring nodes are identified by identity, not by text comparison: this
    module's own prose quotes `INSERT INTO role_assignments` while explaining
    why the surface was missing, and a text-shaped exclusion would either
    swallow a real offender or trip over its own explanation.
    """
    tree = ast.parse(source)
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_nodes
    ]


class TestTheHandlersDoNotReimplementTheDomain:
    def _source(self):
        text = ADMIN_MODULE.read_text(encoding="utf-8")
        assert text.strip(), "admin_endpoints.py is empty -- every row below would pass vacuously"
        return text

    def test_no_sql_is_written_in_the_router(self):
        """
        STRING LITERALS ONLY, FROM THE AST — a comment or a docstring
        mentioning `INSERT` is not a statement and is invisible to this walk
        by construction, which is why it is an AST walk and not a grep.
        """
        verbs = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM")
        offenders = [
            value[:80]
            for value in _non_docstring_strings(self._source())
            if any(verb in value.upper() for verb in verbs)
        ]
        assert offenders == [], offenders

    def test_the_twin_the_sql_scan_finds_a_statement_that_is_not_a_docstring(self, tmp_path):
        """
        Standing Rule 4. The row above is an ABSENCE assertion and is
        satisfied by a scanner that excludes everything. This proves it
        excludes docstrings ONLY: a module whose docstring quotes a statement
        AND whose body contains one reports exactly the body's.
        """
        probe = tmp_path / "probe.py"
        probe.write_text(
            '"""Prose that quotes INSERT INTO role_assignments and explains it."""\n'
            'QUERY = "SELECT user_id FROM users WHERE org_id = %s"\n',
            encoding="utf-8",
        )
        found = [
            value
            for value in _non_docstring_strings(probe.read_text(encoding="utf-8"))
            if "SELECT " in value.upper() or "INSERT INTO" in value.upper()
        ]
        assert found == ["SELECT user_id FROM users WHERE org_id = %s"], found

    def test_the_router_never_resolves_a_role_itself(self):
        """
        DO NOT CREATE A SECOND PERMISSION SYSTEM. The role gate is
        `DataAccess._require_role`, called inside the domain method. A
        handler that read roles and decided would be a second answer, and the
        day the two disagreed the HTTP one would win silently.
        """
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("_require_role", "get_user_roles"), ast.unparse(node)
            if isinstance(node, ast.Name):
                assert node.id not in ("ROLES", "HUMAN_ASSIGNABLE_ROLES"), node.id

    def test_the_router_imports_the_session_dependency_rather_than_defining_one(self):
        """
        The single authentication answer. A `def _get_session` in this module
        would be a second session policy on the same principal.
        """
        tree = ast.parse(self._source())
        defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "_get_session" not in defined, defined
        assert "_get_data_access" not in defined, defined

        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "clinical.endpoints"
            for alias in node.names
        }
        assert {"_get_session", "_get_data_access"} <= imported, imported

    def test_the_router_sets_no_cookie_and_mints_no_session(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("set_cookie", "delete_cookie", "login", "patient_login"), ast.unparse(
                    node
                )
