"""
clinical/app.py
───────────────
THE CLINICAL SERVICE'S APPLICATION OBJECT -- the thing that was missing.

`clinical/endpoints.py` has defined a router since before F1, and F1/F2 wired
its three routes to a real session and a request-scoped `DataAccess`. But
`include_router` appeared exactly once in the whole repository, inside
`clinical/tests/test_exception_endpoints_http.py`, where that file's own
docstring said the app was "test-local and deliberately so ... when the real
app lands, these tests should point at it instead". THIS IS THAT APP, and the
test file now points at it.

So the three routes were reachable only from a test. A router nobody mounts
is the same shape of defect as a placeholder nobody implements: every layer
looks present, and the service does not exist.

WHY THE READINESS ROUTE TOUCHES THE DATABASE, WHICH IS THE WHOLE POINT OF IT
A service that boots and returns 500 on every request passes any liveness
probe. That is not hypothetical here -- it is exactly what this service would
have done before F1, when both dependencies raised `NotImplementedError`: the
process would have been up, `/healthz` would have answered 200, and every
real request would have been a 500. A readiness check that only proves the
process is alive measures something real and it is not the thing.

The precedent in this repository is the weaker kind: `kim_pipeline/api/main.py`
`/api/v1/ready` returns `{"status": "ready"}` unconditionally, touching
nothing it claims readiness for. `/readyz` here opens a connection and runs
real SQL, so an unreachable database, a missing DSN, or a database that is up
but was never bootstrapped all return 503 and name which one.

LIVENESS AND READINESS ARE KEPT APART BECAUSE THEY ANSWER TO DIFFERENT
ACTIONS. `/healthz` says "this process is running" -- if it fails, restart the
container. `/readyz` says "this service can do its job" -- if it fails, stop
sending it traffic, because restarting will not fix a database that is down.
Collapsing them means a database outage restart-loops every replica.

NO API-KEY LAYER, DELIBERATELY. `geper/api/main.py`'s `_require_api_key` is
the repository's ingress pattern and it is FAIL-OPEN by design: with
`GEPER_API_KEYS` unset it allows every request ("dev mode"). That is a
defensible trade for a structure-lookup API and it is the wrong default for
patient data. The clinical routes already authenticate per request through
`_get_session`, which fails CLOSED -- no session, no answer. Adding a
fail-open layer above a fail-closed one would only create a configuration in
which the outer check is off and looks on.
"""

from __future__ import annotations

import os
from typing import Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from clinical.endpoints import router

_DSN_ENV = "CLINICAL_DSN"

# A probe must answer or fail quickly: a readiness check that hangs is
# indistinguishable to an orchestrator from one that is slow to say yes, and
# the default libpq behaviour is to wait far longer than any probe interval.
_PROBE_CONNECT_TIMEOUT_SECONDS = 3

# Bootstrapped by clinical/bootstrap.py from clinical/schema.sql. Its presence
# separates "Postgres is reachable" from "this is the clinical database",
# which is the S4 defect exactly: a server can be up and answer SELECT 1 while
# holding no schema at all.
_SENTINEL_TABLE = "sessions"

app = FastAPI(
    title="GEPER Clinical API",
    version="1.0.0",
    description="Clinical ordering, consent and exception handling.",
)

app.include_router(router)


@app.get("/healthz", tags=["health"], summary="Liveness probe")
async def healthz() -> Dict[str, str]:
    """
    Is this PROCESS alive? Nothing more, and the docstring says so because the
    temptation is to make it say more.

    It deliberately touches no dependency. A liveness probe that fails when
    the database is down tells the orchestrator to restart a healthy process,
    which cannot fix the database and will restart every replica in a loop.
    """
    return {"status": "alive"}


@app.get("/readyz", tags=["health"], summary="Readiness probe (touches the database)")
async def readyz() -> JSONResponse:
    """
    Can this SERVICE actually serve? Returns 503 when it cannot, and names
    which of the three ways it failed.

    Checked in order, because each step is meaningless without the one before:
      1. `CLINICAL_DSN` is configured at all;
      2. a connection can be opened and `SELECT 1` answered;
      3. the schema is present -- otherwise Postgres is up and this is not the
         clinical database.
    """
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        return _not_ready("dsn_not_configured", f"{_DSN_ENV} is not set.")

    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=_PROBE_CONNECT_TIMEOUT_SECONDS) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute("SELECT to_regclass(%s)", (_SENTINEL_TABLE,))
                row = cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 -- a probe reports every failure as not-ready
        # The exception TYPE, never its text: a DSN can carry a password and a
        # libpq error message quotes the connection string back.
        return _not_ready("database_unreachable", type(exc).__name__)

    if row is None or row[0] is None:
        return _not_ready(
            "schema_missing",
            f"connected, but table {_SENTINEL_TABLE!r} does not exist; run clinical.bootstrap.",
        )

    return JSONResponse(status_code=200, content={"status": "ready", "database": "ok", "schema": "ok"})


def _not_ready(reason: str, detail: str) -> JSONResponse:
    """503, not 200-with-a-status-field: an orchestrator reads the code."""
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": reason, "detail": detail},
    )
