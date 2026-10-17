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

THE INTERACTIVE DOCUMENTATION IS OFF UNLESS SOMEBODY TURNS IT ON (w132).
FastAPI mounts `/openapi.json`, `/docs`, `/docs/oauth2-redirect` and `/redoc`
by default, and nobody in this repository ever declared them -- so nobody
decided they were public. They were, and they are the largest anonymous
disclosure on the service: `/openapi.json` alone hands out every path, every
method, every response model field by field (`PortalPatientResponse` ->
patient_id, name, dob, sex), every declared status code and every handler
docstring, to a caller holding no credential.

THE DOCSTRINGS AND RESPONSE MODELS STAY IN THE CODE. What goes is the
anonymous SURFACE, not the documentation: `openapi_url=None` unmounts the
route, it does not delete a schema. Set `CLINICAL_ENABLE_DOCS=1` and all four
come back, unchanged, which is why this is a gate and not a deletion -- a
deletion invites the next developer to re-add them without the gate.

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

from clinical import migrate
from clinical.admin_endpoints import router as admin_router
from clinical.clinic_endpoints import router as clinic_router
from clinical.auth_endpoints import router as auth_router
from clinical.endpoints import router, session_router
from clinical.portal_endpoints import router as portal_router
from clinical.review_endpoints import router as review_router
from clinical.workflow_endpoints import router as workflow_router

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

# w132 fix A. Read at import, once, so the served surface cannot change under
# a running process. "1", "true" and "yes" are accepted; anything else, and
# the absence of the variable, leaves all four documentation routes unmounted.
_DOCS_ENV = "CLINICAL_ENABLE_DOCS"
_DOCS_ENABLED = os.environ.get(_DOCS_ENV, "").strip().lower() in {"1", "true", "yes"}

app = FastAPI(
    title="GEPER Clinical API",
    version="1.0.0",
    description="Clinical ordering, consent and exception handling.",
    # None UNMOUNTS the route. The schema is still built on demand by
    # `app.openapi()`, so the response models and docstrings below are intact
    # and a developer with the flag set sees exactly what they saw before.
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

app.include_router(router)

# w130. The patient portal, mounted ALONGSIDE the staff router and never
# through it. Its routes depend on _get_patient_session, which is a separate
# dependency reading a separate cookie and validating against a separate
# session table -- clinical.endpoints._get_session is neither imported by it
# nor widened. See clinical/portal_endpoints.py for why the alternative (one
# dependency accepting either principal) is the cheap move that silently
# destroys per-patient isolation.
app.include_router(portal_router)

# w132. The authentication surface: POST /auth/login and POST /portal/login,
# in ONE module with NO router prefix, each route declaring its full path.
# Two routes rather than one because the route is what names the principal —
# a combined endpoint could not avoid disclosing which credential store an
# address lives in, on a timing channel that no equalisation inside either
# method reaches. See clinical/auth_endpoints.py.
#
# Mounted alongside, never through: it adds no dependency to the two routers
# above, and neither of them imports it.
app.include_router(auth_router)

# w132c. `POST /auth/logout`. It is a SECOND, UNPREFIXED router out of
# clinical/endpoints.py rather than a third route in auth_endpoints.py,
# because logout is the one authentication act that CONSUMES a session
# instead of minting one — it depends on `_get_session`, which lives there,
# and `auth_endpoints.py` is pinned by a structural guard not to import
# either session dependency. `POST /portal/logout` is on `portal_router`
# above for the mirror reason: `_get_patient_session` lives in that file.
app.include_router(session_router)

# w152 (WAVE 2). THE ADMINISTRATIVE SURFACE, and it is the first one this
# service has ever had: at e1be7ab the seventeen production routes contained
# ZERO administrative ones, which is why fourteen domain capabilities were
# unreachable for the SAME reason — no route.
#
# Mounted ALONGSIDE, never through, exactly as the portal and auth routers
# are. It adds no dependency to any router above and none of them imports it.
# What it DOES import, deliberately, is `clinical.endpoints`'s `_get_session`
# and `_get_data_access`: the administrative routes authenticate with the
# SAME staff dependency as the exception routes and authorise through the
# SAME `DataAccess._require_role` inside the same domain methods. A second
# copy of either would be a second security policy, and the weaker one would
# be the one nobody reads.
#
# NOTHING HERE IS REACHABLE BY A REAL DEPLOYMENT YET. Every administrative
# route requires an Administrator or an Auditor, and no production path makes
# the first one — `assign_role` requires an existing Administrator and is the
# only human-role writer. That deadlock is Wave 3's. Until it lands these
# routes are IMPLEMENTED + UNREACHABLE and are classified that way.
app.include_router(admin_router)

# w157 (WAVE 7). THE LABORATORY BENCH SURFACE -- the Sample and QC stages of
# the `Order -> ... -> Report` chain, and the first routes this service has
# ever served for any operational stage of it.
#
# Measured at 764d646 by an ALIAS-RESOLVING ast.Call census over every non-test
# module: `receive_sample` and `record_qc` each had ZERO production call sites,
# while both were gated, state-checking, audited and tested at the domain
# layer. The missing link was the outermost one, as it was in thirteen of the
# fourteen capabilities w150 counted.
#
# Mounted ALONGSIDE, never through, exactly as the portal, auth and
# administrative routers are. It adds no dependency to any router above and
# none of them imports it. What it DOES import is `clinical.endpoints`'s
# `_get_session`/`_get_data_access` and `clinical.admin_endpoints`'s three
# refusal helpers -- the SAME staff dependency and the SAME three refusals, so
# there is no second security policy and no second refusal vocabulary.
#
# NEITHER ROUTE IS REACHABLE BY A REAL DEPLOYMENT YET, and the reason is
# upstream of them: the first-administrator deadlock is unchanged, and the
# ORDER stage they act on has no production entry point at all -- `create_order`
# requires a `test_id` foreign key into `tests`, a table with no DataAccess
# writer of any kind. Both are IMPLEMENTED + UNREACHABLE and are classified
# that way. See clinical/workflow_endpoints.py for the full measurement and for
# what this wave STOPPED, release included.
app.include_router(workflow_router)

# w162. THE ORDERING CLINICIAN'S SURFACE -- patient registration and consent,
# the two acts that precede an order, and the THIRD prefix this service
# serves.
#
# Measured at 44d624b by an ast.Call census over every non-test module:
# `create_patient`, `record_consent` and `withdraw_consent` each had ZERO
# production call sites AND no `_require_role` of any kind, while each already
# took a Session and bound `org_id` from it. w155 met all three and correctly
# REFUSED to invent a role without a ruling. The human ruled on 2026-09-19 --
# the ordering clinician owns both, not a mandatory Administrator and never a
# Lab Technician -- so the gate is now `_require_role(session, "Orderer")`
# inside each method, as its first executable statement, and these three
# routes are the outermost link that was missing.
#
# WHY A THIRD PREFIX rather than `/admin` or `/lab`: the prefix names the
# SURFACE and never the gate (`/admin/audit` is Auditor, retraction is
# Approver, `/lab` is Lab technician). `/admin` would encode in every access
# log and route census the reading the ruling rejects -- that registering a
# patient is an administrative act -- and `/lab` names the bench, whose role
# the ruling explicitly bars from creating a patient identity. `/clinic` is
# also where the Order stage belongs when the `tests` catalogue unblocks it.
#
# Mounted ALONGSIDE, never through, exactly as every router above. It adds no
# dependency to any of them and none of them imports it. What it DOES import
# is `clinical.endpoints`'s `_get_session`/`_get_data_access` and
# `clinical.admin_endpoints`'s three refusal helpers -- the SAME staff
# dependency and the SAME refusals, so there is no second security policy and
# no second refusal vocabulary.
#
# THE FIRST-ADMINISTRATOR DEADLOCK IS UNCHANGED, so an Orderer session is
# still exactly as reachable as an operator invoking `provision_organisation`
# directly. What has changed is that these three verbs are no longer
# unreachable BY DESIGN. See clinical/clinic_endpoints.py.
app.include_router(clinic_router)

# Stage 3. THE REVIEW SURFACE -- claim, submit, approve, release -- the FOURTH
# prefix this service serves. Mounted ALONGSIDE, never through, like every
# router above: it adds no dependency to any of them and none imports it. It
# imports `_get_session`/`_get_data_access` and the three refusal helpers, so
# there is no second security policy and no second refusal vocabulary. The
# gates are the domain methods'. See clinical/review_endpoints.py.
app.include_router(review_router)


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
         clinical database;
      4. the schema is at the VERSION this code was written for (w126m).

    WHY STEP 4 EXISTS, AND IT IS THE WHOLE OF THIS WAVE'S PRODUCTION CHANGE.
    `_SENTINEL_TABLE` is `sessions`, and `sessions` exists in every variant of
    this schema there has ever been -- it is a baseline table no held change
    touches. So steps 1-3 answered 200 for EVERY wrong pairing of code and
    database: a service whose code names a column its database does not have
    reported healthy to Docker's HEALTHCHECK and to any orchestrator, and then
    failed the reads that column is in. Step 3 detects ABSENCE; it has never
    been able to detect a VERSION, and the two are different questions.

    Step 4 asks `clinical/migrate.py` instead, which answers from the
    migration history AND from `pg_catalog` -- so a database that is behind,
    that has never been migrated, whose migration files have been edited since
    they ran, or whose history claims an object the catalog does not have, is
    all reported NOT ready, each by its own name.

    IT DETECTS, IT DOES NOT FIX. Migrating needs privileges the entire
    GRANT/REVOKE block exists to deny `clinical_app`, and `clinical_app` is
    what this connection is. `python -m clinical.migrate` is the other half
    and it is run by an operator with the administrative DSN.

    THE 200 BODY IS UNCHANGED, deliberately: this is a probe other things
    parse, and a new field in the ready response is endpoint behaviour that
    nothing asked to change. Everything new here is a 503.
    """
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        return _not_ready("dsn_not_configured", f"{_DSN_ENV} is not set.")

    import psycopg

    state = None
    try:
        with psycopg.connect(dsn, connect_timeout=_PROBE_CONNECT_TIMEOUT_SECONDS) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute("SELECT to_regclass(%s)", (_SENTINEL_TABLE,))
                row = cursor.fetchone()
            if row is not None and row[0] is not None:
                # Its own try: a failure HERE is not the database being
                # unreachable, and labelling it that way would send an
                # operator to look at the network. Either way it is 503 --
                # a probe that cannot establish the version must not report
                # ready, which is the same fail-closed rule as the rest.
                try:
                    state = migrate.inspect_state(connection)
                except Exception as exc:  # noqa: BLE001
                    return _not_ready("schema_state_unreadable", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 -- a probe reports every failure as not-ready
        # The exception TYPE, never its text: a DSN can carry a password and a
        # libpq error message quotes the connection string back.
        return _not_ready("database_unreachable", type(exc).__name__)

    if row is None or row[0] is None:
        return _not_ready(
            "schema_missing",
            f"connected, but table {_SENTINEL_TABLE!r} does not exist; run clinical.bootstrap.",
        )

    # w126m. `state.reason` and `state.detail` name migration versions and
    # object names out of this repository's own files -- never a DSN, never a
    # row, never a credential.
    if state is not None and not state.ok:
        return _not_ready(state.reason, state.detail)

    return JSONResponse(status_code=200, content={"status": "ready", "database": "ok", "schema": "ok"})


def _not_ready(reason: str, detail: str) -> JSONResponse:
    """503, not 200-with-a-status-field: an orchestrator reads the code."""
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": reason, "detail": detail},
    )
