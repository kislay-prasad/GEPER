"""
clinical/tests/test_service_probes.py
─────────────────────────────────────
THE READINESS PROBE'S NEGATIVE CONTROL, WHICH IS THE ONLY REASON THE PROBE IS
WORTH HAVING.

A readiness endpoint that returns 200 is evidence of nothing until you have
watched it return 503. `kim_pipeline/api/main.py`'s `/api/v1/ready` returns
`{"status": "ready"}` unconditionally and would pass a "does /readyz answer
200?" test forever, on a machine with no database at all.

So every test here that asserts ready is paired with one that breaks exactly
one precondition and asserts NOT ready -- and asserts the REASON, because
three different failures returning one indistinguishable 503 would send an
operator to the wrong place.

The liveness/readiness split is tested too: `/healthz` must keep answering 200
while the database is unreachable. If it did not, a database outage would
restart-loop every replica instead of draining traffic from them.
"""

import os
import pathlib

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"

# A port nothing listens on: refused fast, rather than a hostname that would
# make the test depend on how this machine resolves unknown names.
UNREACHABLE_DSN = "postgresql://postgres:nobody@127.0.0.1:1/postgres"


@pytest.fixture()
def client():
    pytest.importorskip("fastapi", reason="fastapi not installed")
    pytest.importorskip("httpx", reason="httpx not installed (starlette TestClient requires it)")
    from fastapi.testclient import TestClient

    from clinical.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def bootstrapped(monkeypatch):
    """A real bootstrapped database with CLINICAL_DSN pointing at it."""
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


class TestLivenessIsAboutTheProcessOnly:
    def test_healthz_is_200_when_the_service_is_up(self, client, bootstrapped):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_healthz_stays_200_when_the_database_is_unreachable(self, client, monkeypatch):
        """
        THE SPLIT, ASSERTED. Liveness must not follow the database down: a
        failing liveness probe tells the orchestrator to restart the process,
        which cannot fix a database outage and would loop every replica.
        """
        monkeypatch.setenv("CLINICAL_DSN", UNREACHABLE_DSN)
        assert client.get("/healthz").status_code == 200

    def test_healthz_stays_200_with_no_dsn_at_all(self, client, monkeypatch):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        assert client.get("/healthz").status_code == 200


class TestReadinessTouchesTheDatabase:
    def test_readyz_is_200_against_a_bootstrapped_database(self, client, bootstrapped):
        response = client.get("/readyz")
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ready", "database": "ok", "schema": "ok"}

    def test_readyz_is_503_when_the_database_is_unreachable(self, client, monkeypatch):
        """
        THE NEGATIVE CONTROL. Without this, a probe that never checks anything
        passes the positive test forever.
        """
        pytest.importorskip("psycopg", reason="psycopg not installed")
        monkeypatch.setenv("CLINICAL_DSN", UNREACHABLE_DSN)
        response = client.get("/readyz")
        assert response.status_code == 503, response.text
        assert response.json()["status"] == "not_ready"
        assert response.json()["reason"] == "database_unreachable"

    def test_readyz_is_503_when_no_dsn_is_configured(self, client, monkeypatch):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        response = client.get("/readyz")
        assert response.status_code == 503, response.text
        assert response.json()["reason"] == "dsn_not_configured"

    def test_readyz_is_503_when_postgres_is_up_but_holds_no_schema(self, client, bootstrapped):
        """
        THE S4 CASE. A reachable server is not a bootstrapped one, and a probe
        that stops at `SELECT 1` would call this ready.
        """
        import psycopg

        connection = psycopg.connect(bootstrapped, autocommit=True, connect_timeout=10)
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        connection.close()

        response = client.get("/readyz")
        assert response.status_code == 503, response.text
        assert response.json()["reason"] == "schema_missing"

    def test_the_failure_detail_never_contains_the_dsn(self, client, monkeypatch):
        """
        A libpq error quotes the connection string back, and a DSN carries a
        password. The probe reports the exception TYPE for that reason.
        """
        monkeypatch.setenv("CLINICAL_DSN", UNREACHABLE_DSN)
        body = client.get("/readyz").text
        assert "nobody" not in body
        assert "postgresql://" not in body


class TestTheRouterIsMountedOnTheRealApp:
    def test_the_exception_routes_exist_on_the_service(self, client, bootstrapped):
        """
        Before this change `include_router` appeared once in the repository,
        in a test file. The routes existed and nothing served them.

        Takes `bootstrapped` so the DSN is configured: without it these routes
        answer 503 rather than 401, because `_get_session` depends on
        `_get_data_access` and an unconfigured service outranks an
        unauthenticated caller. Correct, but it would not prove a mount.
        """
        # Asserted by REQUESTING the routes rather than by reading the app's
        # route table: an unmounted route 404s, and a mounted one that needs a
        # session 401s. 401 is therefore the proof -- it can only come from
        # the router's own dependency, which nothing but a mount can reach.
        for path in ("/exceptions/worklist", "/exceptions/orders/00000000-0000-0000-0000-000000000000"):
            response = client.get(path)
            assert response.status_code == 401, f"{path} -> {response.status_code}, expected 401"
            assert response.json()["detail"] == "Authentication failed."

        patched = client.patch(
            "/exceptions/00000000-0000-0000-0000-000000000000",
            json={"resolution_action": "retry_check", "resolution_note": "x"},
        )
        assert patched.status_code == 401, patched.text
