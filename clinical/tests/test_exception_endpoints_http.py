"""
clinical/tests/test_exception_endpoints_http.py
───────────────────────────────────────────────
THE FIRST HTTP-LEVEL TEST IN THIS REPOSITORY. Not a gap filled in an existing
file -- a layer that had never been exercised at all.

`test_exception_endpoints.py` sits next to this one and, despite its name,
contains no `TestClient`, no app and no `dependency_overrides`: all eleven of
its tests call `DataAccess` methods directly. It is a good data-layer test file
wearing an endpoint name. That is why F1 survived inside a file named for the
thing it broke -- `_get_session` and `_get_data_access` both raised
`NotImplementedError` on every request, and nothing asked. A test file named
for a layer it does not touch is worse than no test file, because it answers
"is this covered?" with a yes.

WHAT THIS FILE PINS, and the three claims are kept apart because they fail
independently:

  1. THE ROUTES ANSWER AT ALL. Each of the three is called through a real
     TestClient against a real bootstrapped database and gets a real response.
     Before the change every one of them was a 500.
  2. THE ACTOR CANNOT BE ASSERTED BY THE CALLER (F2). Sending an `actor` in
     the body does not make it the actor -- the recorded one is the session's
     user, and the caller's string appears nowhere.
  3. `DataAccess` IS REQUEST-SCOPED (the ruling). Two sequential requests get
     two different instances. This is the only test that stops someone
     helpfully re-adding the singleton the old docstring asked for, which
     would silently reintroduce cross-request transaction bleed.

THE APP IS NO LONGER TEST-LOCAL. This file used to build its own `FastAPI()`
and mount the router, because no clinical application object existed; it said
so, and said the swap would be a one-line change. `clinical/app.py` now exists
and the swap has been made. Everything below therefore runs against THE OBJECT
THE SERVICE ACTUALLY SERVES, not a stand-in that happens to mount the same
router -- and for as long as the stand-in was the only `include_router` in the
repository, these nine tests were green against a service that did not exist.
"""

import datetime
import os
import pathlib
import uuid

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
USER_PASSWORD = "password"
USER_EMAIL = "admin@org-a.test"


@pytest.fixture()
def clinical_dsn(monkeypatch):
    """
    A bootstrapped database, and `CLINICAL_DSN` pointing at it.

    Uses `clinical.bootstrap` rather than the neighbouring fixture's hand-rolled
    CREATE ROLE, so the roles here are the LOGIN roles a deployment would get.
    The endpoints read the DSN from the environment at request time, which is
    what makes `monkeypatch.setenv` the whole of the wiring.
    """
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


@pytest.fixture()
def seeded(clinical_dsn):
    """
    An org, an admin user with a real login session, an order, and one open
    exception -- created through DataAccess on a connection of its own, so the
    request-scoped connections the endpoints open see committed data.

    Returns the session, the order id and the exception id.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess, SystemClock
    from clinical.models.exception import ExceptionCategory, ExceptionReasonCode

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))

    org_id = dao.create_organisation("Org A")
    user_id = dao.create_user(org_id, USER_EMAIL, USER_PASSWORD)
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with connection.cursor() as cur:
        # Direct insert to break the bootstrap cycle, exactly as the
        # neighbouring data-layer fixture does.
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, now),
        )
        test_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Test Gene Panel", "GRCh38", "active", now),
        )
    connection.commit()

    session = dao.login(USER_EMAIL, org_id, USER_PASSWORD)
    patient_id = dao.create_patient(session, "Test Patient", datetime.date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session, patient_id, "testing")
    order_id = dao.create_order(
        session,
        patient_id=patient_id,
        test_id=test_id,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
    )
    exception_id = dao.create_or_reopen_exception(
        session,
        order_id,
        ExceptionCategory.PRECONDITION_FAILURE.value,
        ExceptionReasonCode.CONSENT_MISSING.value,
        "Consent not provided",
        "orderer",
        "orderer-1",
    )
    connection.commit()
    connection.close()
    return session, order_id, exception_id


@pytest.fixture()
def client(clinical_dsn):
    """
    A TestClient over THE REAL APPLICATION OBJECT, `clinical.app.app`.

    This fixture used to build a test-local `FastAPI()` and mount the router
    itself, because no clinical application object existed. One does now, and
    the swap was the one-line change this docstring promised. It matters more
    than it looks: a test-local app proves the router works when mounted, not
    that anything mounts it -- and for as long as that was the only
    `include_router` in the repository, these nine tests passed against a
    service that did not exist.
    """
    pytest.importorskip("fastapi", reason="fastapi not installed")
    # httpx is named separately because starlette's TestClient raises a
    # RuntimeError -- not an ImportError -- when it is missing, so
    # importorskip("fastapi") alone would let a missing httpx surface as an
    # unexplained error at setup rather than as a named missing package.
    pytest.importorskip("httpx", reason="httpx not installed (starlette TestClient requires it)")
    from fastapi.testclient import TestClient

    from clinical.app import app

    with TestClient(app) as test_client:
        yield test_client


def _auth(session):
    return {"Authorization": f"Bearer {session.session_id}"}


class TestTheRoutesAnswerAtAll:
    """Before F1 every one of these was a 500 from a raising dependency."""

    def test_get_exceptions_for_order(self, client, seeded):
        session, order_id, exception_id = seeded
        response = client.get(f"/exceptions/orders/{order_id}", headers=_auth(session))
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == str(exception_id)
        assert body[0]["status"] == "open"

    def test_get_worklist(self, client, seeded):
        session, _order_id, exception_id = seeded
        response = client.get("/exceptions/worklist", params={"owner": "orderer"}, headers=_auth(session))
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == [str(exception_id)]

    def test_resolve_exception(self, client, seeded):
        session, _order_id, exception_id = seeded
        response = client.patch(
            f"/exceptions/{exception_id}",
            json={"resolution_action": "retry_check", "resolution_note": "Consent obtained"},
            headers=_auth(session),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "resolved"


class TestAuthenticationIsRequired:
    def test_no_credentials_is_401(self, client, seeded):
        _session, order_id, _exception_id = seeded
        response = client.get(f"/exceptions/orders/{order_id}")
        assert response.status_code == 401
        assert response.json()["detail"] == "Authentication failed."

    def test_unknown_session_is_401_with_the_same_message(self, client, seeded):
        """
        A well-formed but unknown id must be indistinguishable from a malformed
        one: telling them apart would tell an unauthenticated caller which
        session ids exist.
        """
        _session, order_id, _exception_id = seeded
        unknown = client.get(
            f"/exceptions/orders/{order_id}",
            headers={"Authorization": f"Bearer {uuid.uuid4()}"},
        )
        malformed = client.get(
            f"/exceptions/orders/{order_id}",
            headers={"Authorization": "Bearer not-a-uuid"},
        )
        assert unknown.status_code == malformed.status_code == 401
        assert unknown.json() == malformed.json()

    def test_cookie_is_accepted_as_well_as_the_header(self, client, seeded):
        session, order_id, _exception_id = seeded
        client.cookies.set("clinical_session", str(session.session_id))
        try:
            response = client.get(f"/exceptions/orders/{order_id}")
            assert response.status_code == 200, response.text
        finally:
            client.cookies.clear()


class TestACallerCannotAssertAnActorTheyAreNot:
    """
    F2. The recorded actor is the authenticated user, and the caller has no way
    to say otherwise -- the field is gone rather than validated, because a
    validated claim is still a claim.
    """

    def test_recorded_actor_is_the_session_user_not_the_body(self, client, seeded):
        session, order_id, exception_id = seeded
        response = client.patch(
            f"/exceptions/{exception_id}",
            json={
                "resolution_action": "retry_check",
                "resolution_note": "Consent obtained",
                # A caller trying to attribute the action to someone else.
                "actor": "somebody-else",
            },
            headers=_auth(session),
        )
        assert response.status_code == 200, response.text

        detail = client.get(f"/exceptions/orders/{order_id}", headers=_auth(session)).json()[0]
        assert detail["last_resolved_by"] == str(session.user_id)
        assert detail["last_resolved_by"] != "somebody-else"

        resolve_events = [event for event in detail["events"] if event["action"] == "resolve"]
        assert len(resolve_events) == 1
        assert resolve_events[0]["actor"] == str(session.user_id)
        assert "somebody-else" not in response.text

    def test_actor_is_not_required_and_its_absence_is_not_an_error(self, client, seeded):
        """The field is gone, so omitting it must not 400 the way it used to."""
        session, _order_id, exception_id = seeded
        response = client.patch(
            f"/exceptions/{exception_id}",
            json={"resolution_action": "retry_check", "resolution_note": "Consent obtained"},
            headers=_auth(session),
        )
        assert response.status_code == 200, response.text


class TestDataAccessIsRequestScoped:
    """
    The ruling was (A) request-scoped, against the old docstring's "cache as
    singleton". A shared instance would reintroduce cross-request transaction
    bleed via `transactional`'s instance-level `_in_transaction` flag, and no
    other test in the repository would notice. This one would.
    """

    def test_two_sequential_requests_get_different_instances(self, client, seeded, monkeypatch):
        import clinical.endpoints as endpoints

        seen = []
        real = endpoints.DataAccess

        class Recording(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                seen.append(id(self))

        monkeypatch.setattr(endpoints, "DataAccess", Recording)

        session, order_id, _exception_id = seeded
        assert client.get(f"/exceptions/orders/{order_id}", headers=_auth(session)).status_code == 200
        assert client.get(f"/exceptions/orders/{order_id}", headers=_auth(session)).status_code == 200

        assert len(seen) == 2, f"expected one DataAccess per request, got {len(seen)}"
        assert seen[0] != seen[1], "both requests shared one DataAccess -- the singleton is back"
