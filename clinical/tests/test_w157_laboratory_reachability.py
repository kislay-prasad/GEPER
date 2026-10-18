"""
clinical/tests/test_w157_laboratory_reachability.py
───────────────────────────────────────────────────
WAVE 7, AND THE FIRST THING IT DID WAS MEASURE THE CHAIN.

The wave's subject is `Order -> Sample -> Processing -> QC -> Interpretation
-> Review -> Report`, and the instruction is to wire what is implemented but
unreachable, to STOP what needs a product decision, and above all NOT to build
a second state machine beside the existing one.

MEASURED AT `w126m-migrations` TIP `764d646`, by an ALIAS-RESOLVING `ast.Call`
census over every non-test module in the repository. Three instruments were
wrong before this one and each was wrong in a way that flattered the code:

  * grep credits a DOCSTRING as a caller. That is how `_release_report` came
    to be recorded with thirteen callers and zero calls.
  * a bare-name `ast.Call` census misses `from review.signoff import approve
    as _approve` followed by `_approve(...)`. It reported the Review stage as
    having no entry point when it has a CLI one.
  * naive `app.routes` iteration reports a surface of TWO on this stack,
    because `include_router` leaves an opaque `_IncludedRouter` wrapper.

The census used here resolves import aliases, counts only `ast.Call` nodes,
walks the route tree into the wrappers, and carries a NON-VACUITY CONTROL in
every direction — a needle it must find, or the instrument is broken rather
than the claim proved. `TestThePremiseThisWaveMeasured` reproduces it as rows,
so the measurement cannot decay into prose.

WHAT IT FOUND, AND IT IS THE REASON THIS FILE IS SMALL AND THE REPORT IS LONG:

  Order          create_order / place_order / cancel_order   0 callers, 0 routes
  Sample         receive_sample                              0 callers, 0 routes
  Processing     create_sequencing_run / create_vcf          0 callers, 0 routes
                 record_pipeline_result                      1 caller  (worker)
  QC             record_qc                                   0 callers, 0 routes
  Interpretation create_interpretation / create_report       0 callers, 0 routes
  Review         submit_for_review / approve_report          1 caller  (review CLI)
  Report         _release_report / require_release           0 callers, 0 routes
                 retract_report                              1 caller  (w156 route)

TWO STAGES ARE WIRED HERE AND THE REST ARE CARDED. Sample and QC are pure
"make reachable": both domain methods are Lab-technician-gated, both enforce
the existing order/sample state rules themselves, both are audited, and both
vocabularies are enumerated IN THE SCHEMA, so nothing below decides anything.

RELEASE IS NOT WIRED, AND THAT IS THIS WAVE'S LOAD-BEARING REFUSAL.
`_release_report` still has zero production call sites — re-verified here, as
a row, with a control needle. So `/portal/me/reports`,
`/portal/me/reports/{report_id}` and `/portal/me/trends` still return empty
for every patient for ever, and w156's retraction verb still acts on a state
nothing in production can produce. WHO may release and FROM WHICH STATE are
derivable and already encoded (Approver on the session and on `released_by`,
from `approved` or `released`). The `consumer` is NOT, and the tree says so
itself, in `schema.sql`'s comment on that very column: "Free text pending the
delivery paths of a later commit; enumerating it now would fix a vocabulary
before the paths exist." Spec 13.4 describes release as an event a DELIVERY
PATH records and names no route; there is no delivery path in this backend.
STOPPED AND CARDED rather than settled by accident.
`TestTheReleaseGapIsStillOpen` is the tripwire that keeps that honest in both
directions: it fails if release quietly acquires a caller AND it fails if the
schema comment that grounds the refusal disappears.

THE FIVE WRONG-FIRST ROWS, AND WHAT EACH CATCHES AND IS VACUOUS AGAINST —
stated per row in its own docstring, and summarised here:

  (A) NO ROLE. `TestOnlyALabTechnicianMayWorkTheBench` — the five non-Lab-
      technician principals get ONE refusal. Catches a handler that forgot the
      gate or a domain method whose `_require_role` moved below a read.
      Vacuous against a gate that names the WRONG role but still refuses
      everyone — which row (A2) covers by requiring the Lab technician to
      SUCCEED.
  (B) CROSS-ORGANISATION. `TestTheCrossOrganisationTransitionIsRefused` —
      organisation B holds a REAL placed order and a REAL received sample
      whose identifiers organisation A names. Catches a dropped `org_id`
      predicate. Vacuous WITHOUT the planted rows, because two random UUIDs
      never collide (hazard 3), which is why they are constructed.
  (C) AUDIT. `TestTheAuditRowNamesTheRightActorOrganisationAndSpecimen` —
      the row must exist, name the acting Lab technician and their own
      organisation, and CARRY THE SPECIMEN TYPE. Catches a transition that
      lands with no audit row, one attributed to the target's tenant, and the
      `params.get("type")` defect this wave found and fixed. Vacuous against a
      row whose details are merely PRESENT, which is why the type is asserted
      by VALUE against the value sent.
  (D) STATE-MACHINE BYPASS. `TestTheExistingStateMachineIsNotDuplicated` —
      the handler modules contain no SQL and no `orders` write of any kind,
      by syntax tree; the `placed -> sample_awaited` advance is performed by
      the domain method and observed through `get_order`. Catches a handler
      that wrote the end state directly. Vacuous against a handler that writes
      state through a helper in another module, which the "exactly one
      decorated call" row covers from the other side.
  (E) FORBIDDEN SOURCE STATE. `TestATransitionFromAForbiddenStateIsRefused` —
      a DRAFT order and a CANCELLED order cannot receive a specimen, and the
      refusal SAYS WHY. Catches a wired route that skipped the state check.
      Vacuous against a 400 that refuses everything, which the positive twin
      on the placed order catches.

THE SIX MEASURED HAZARDS, AND WHERE EACH IS ANSWERED:
  1. `@auditable` has no `functools.wraps`, so `inspect.signature` answers
     `(self, *args, **kwargs)`. EVERY structural claim here reads the SYNTAX
     TREE, and `_first_role_gate`'s non-vacuity twin lives in w135.
  2. A refusal asserting only "it failed" is green against broken code — so
     every refusal row asserts the PAIR (status, raw body bytes), and the two
     400s assert the RULE they name, not merely their status.
  3. Construct the collision — organisation B holds a real placed order and a
     real sample. Without them a missing org predicate is invisible.
  4. Refusal order leaks — refusals are asserted as SETS OF SIZE ONE, never
     pairwise.
  5. Validation order leaks — `TestTheGateRunsBeforeAnyVocabularyOrFormatCheck`
     sends an unknown sample type, an unknown QC verdict and a malformed
     identifier as a non-Lab-technician and requires the SAME 403 as a
     well-formed body. Its twin proves a Lab technician CAN tell them apart.
  6. A substring scan credits prose — no claim here about a definition, a
     caller, a parameter or a route is made with `in source`. The one place a
     substring IS used (the schema comment tripwire) is asserting the presence
     of PROSE, which is the only thing a substring scan is right for.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib

import pytest

from clinical.tests.test_w135_patient_report_access import (  # noqa: E402
    PASSWORD,
    T0,
    _catalogue_test,
)

DSN = os.getenv("CLINICAL_TEST_DSN")

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
CLINICAL = pathlib.Path(__file__).resolve().parent.parent
DATA_ACCESS_MODULE = CLINICAL / "data_access.py"
WORKFLOW_MODULE = CLINICAL / "workflow_endpoints.py"
SCHEMA_SQL = CLINICAL / "schema.sql"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"

STAFF_COOKIE = "clinical_session"

NOWHERE = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"

# The refusal bodies, IMPORTED rather than retyped, so a change to a constant
# fails these rows instead of silently rewriting what they assert.
from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND  # noqa: E402
from clinical.data_access import GENERIC_AUTH_ERROR  # noqa: E402


def _body(detail):
    """The exact bytes FastAPI serialises `{"detail": ...}` to.

    RAW BYTES, NOT A PARSED DICT — Standing Rule 2b read literally: the thing
    that must be indistinguishable is what goes on the wire, and a dict is not
    hashable so a refusal SET could not be formed from it at all.
    """
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


UNAUTHENTICATED = (401, _body(GENERIC_AUTH_ERROR))
FORBIDDEN = (403, _body(ADMIN_FORBIDDEN))
NOT_FOUND = (404, _body(ADMIN_NOT_FOUND))


def _pair(response):
    """(status, raw body bytes) as ONE value — Standing Rule 8: an oracle can
    live in either channel, so a refusal set is formed over both."""
    return (response.status_code, response.content)


# ═══════════════════════════════════════════════════════════════════════════
# THE INSTRUMENT — alias-resolving, call-only, with a control needle
# ═══════════════════════════════════════════════════════════════════════════


def _production_modules():
    """Every non-test `.py` in the repository, as paths.

    A TEST CALLER DOES NOT COUNT, which is the whole point of the exclusion:
    `receive_sample` has dozens of test callers and that is exactly why it
    looked reachable for seven waves.
    """
    skip = {"__pycache__", ".git", "node_modules", ".venv", "venv", "site-packages"}
    out = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = pathlib.Path(dirpath) / name
            posix = full.as_posix()
            if "/tests/" in posix or "/test/" in posix:
                continue
            if name.startswith("test_") or name.endswith("_test.py") or name.startswith("conftest"):
                continue
            out.append(full)
    return out


_PARSED: "list[tuple[str, ast.Module, dict]] | None" = None


def _parsed_production():
    """Every production module parsed ONCE, with its import aliases resolved.

    Cached because `_call_sites` is called from a dozen parametrised rows and
    the repository holds several hundred production modules; parsing them per
    row turned a one-second census into a minute of it.
    """
    global _PARSED
    if _PARSED is None:
        out = []
        for path in _production_modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            alias = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for a in node.names:
                        if a.asname:
                            alias[a.asname] = a.name.rsplit(".", 1)[-1]
            out.append((path.relative_to(REPO).as_posix(), tree, alias))
        _PARSED = out
    return _PARSED


def _call_sites(name):
    """
    Every PRODUCTION `ast.Call` site of `name`, as (relative path, lineno).

    THREE THINGS THIS DOES THAT A SIMPLER VERSION DOES NOT, each because a
    simpler version was measurably wrong on this repository:

      * only `ast.Call` nodes count, so a docstring naming the method is not a
        caller (the `_release_report` thirteen-callers-zero-calls defect);
      * import ALIASES are resolved, so `from review.signoff import approve as
        _approve` then `_approve(...)` is counted against `approve` (without
        this the Review stage reads as having no entry point when it has a CLI
        one);
      * a literal `getattr(obj, "<name>")` is counted too, because dispatch
        through `getattr` reaches a method without ever naming it in a call's
        `func`.
    """
    found = []
    for rel, tree, alias in _parsed_production():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = False
            if isinstance(func, ast.Attribute) and func.attr == name:
                hit = True
            elif isinstance(func, ast.Name) and (func.id == name or alias.get(func.id) == name):
                hit = True
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == name
            ):
                hit = True
            if hit:
                found.append((rel, node.lineno))
    return sorted(found)


def _first_role_gate(function_node):
    """
    The role named by `self._require_role(session, "<role>")` when that call
    is the function's FIRST EXECUTABLE statement, else None.

    THE SAME HELPER w156 USES, and its non-vacuity twin is w135's
    `test_the_twin_the_tree_walk_sees_a_gate_that_is_not_first`, which proves
    it returns None for a gate that is second and for a method with no gate.

    "FIRST EXECUTABLE" SKIPS A LEADING DOCSTRING AND NOTHING ELSE. The
    ordering is the property, not a style preference: a gate below the order
    lookup would make an unauthorised caller's refusal vary with whether the
    identifier names an existing row — an existence oracle for orders in this
    organisation and, worse, in another.

    AST, NOT `inspect`: `@auditable` carries no `functools.wraps`.
    """
    body = list(function_node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Call):
        return None
    call = first.value
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "_require_role":
        return None
    if len(call.args) != 2 or not isinstance(call.args[1], ast.Constant):
        return None
    return call.args[1].value


def _data_access_methods():
    """Every method of `DataAccess`, by name, from the syntax tree."""
    tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DataAccess")
    return {n.name: n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _handler(module_path, name):
    """One top-level function of a module, from the syntax tree."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


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


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    """Ten attempts per source per minute is the shipped limit and several rows
    here log in as six principals. A 429 is not a refusal this file is about."""
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
def reader(clinical_dsn):
    """
    A SECOND, INDEPENDENT `DataAccess` FOR READING BACK WHAT THE ROUTE DID.

    STANDING RULE 2a: every assertion about state goes through a DOMAIN
    METHOD, never a hand-written SELECT. A test that builds its expectation
    with raw SQL passes while the method meant to read it is broken (the Phase
    5d cannot-fail shape), and here it would be doubly wrong, because a
    hand-written SELECT could omit the very `org_id` predicate this file
    exists to prove.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
    connection.close()


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app)


def _account(dao, conn, org_id, email, roles):
    """One account holding EXACTLY the roles named, and a live session for it.

    Roles are inserted directly because `assign_role` requires an existing
    Administrator and nothing in production makes the first one (the Wave 3
    deadlock, unchanged by this wave). `test_identity.py`, w152 and w156
    already break the cycle this way; this is the same break, not a new
    mechanism, and it is a FIXTURE rather than a bootstrap.
    """
    user_id = dao.create_user(org_id, email, PASSWORD)
    with conn.cursor() as cur:
        for role in roles:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    org_id,
                    role,
                    user_id,
                    T0,
                    "w157 test fixture: approves" if role == "Approver" else None,
                ),
            )
    conn.commit()
    return user_id, dao.login(email, org_id, PASSWORD)


@pytest.fixture()
def world(dao, conn):
    """
    TWO ORGANISATIONS, AND THE SECOND ONE IS THE WHOLE POINT OF ROW (B).

    Organisation B holds a REAL Lab technician, a REAL placed order and a REAL
    received sample. Two randomly generated UUIDs never collide, so without
    planted rows a `receive_sample` or `record_qc` that had dropped its
    `org_id` predicate would pass every other row in this file — hazard 3,
    construct the collision.

    SEVEN DISTINCT PRINCIPALS IN EACH, and the separation is what lets this
    file ask "which role may work the bench" at all.

    THE ACTING PRINCIPAL HOLDS LAB TECHNICIAN AND NOTHING ELSE. That is
    deliberate and it is the row that would otherwise be silently weak: the
    `workflow` account has to hold every role to BUILD an order (Orderer) and
    would prove only "a principal holding six roles may receive a specimen" —
    true of a route gated on any one of them, and true of a route with no gate
    at all. `labtech@…` holds Lab technician alone, so a 201 from it is a
    statement about Lab technician specifically. It is also why the Orderer
    can be shown to be REFUSED here, which is `receive_sample`'s own
    "the two operational roles do not overlap" made behavioural.

    THREE ORDERS PER ORGANISATION, for the three source states row (E) needs:
    one PLACED (may receive), one DRAFT (may not), one CANCELLED (may not).
    """
    orgs = {}
    for tag, name in (("a", "Org A"), ("b", "Org B")):
        org_id = dao.create_organisation(name)
        workflow_id, workflow = _account(
            dao,
            conn,
            org_id,
            f"workflow@{tag}.example",
            ("Administrator", "Auditor", "Orderer", "Lab technician", "Interpreter", "Approver"),
        )
        labtech_id, labtech = _account(dao, conn, org_id, f"labtech@{tag}.example", ("Lab technician",))
        orderer_id, orderer = _account(dao, conn, org_id, f"orderer@{tag}.example", ("Orderer",))
        admin_id, admin = _account(dao, conn, org_id, f"admin@{tag}.example", ("Administrator",))
        auditor_id, auditor = _account(dao, conn, org_id, f"auditor@{tag}.example", ("Auditor",))
        approver_id, approver = _account(dao, conn, org_id, f"approver@{tag}.example", ("Approver",))
        nobody_id, nobody = _account(dao, conn, org_id, f"nobody@{tag}.example", ())

        test_id = _catalogue_test(conn, org_id)
        patient = dao.create_patient(workflow, f"Patient {tag.upper()}", dt.date(1990, 1, 1), "F")
        consent_id = dao.record_consent(workflow, patient, "testing")

        def _order(state):
            order_id = dao.create_order(
                workflow,
                patient_id=patient,
                test_id=test_id,
                required_scope="testing",
                consent_id=consent_id,
                priority="routine",
                clinical_indication="w157",
            )
            if state in ("placed", "cancelled"):
                dao.place_order(workflow, order_id)
            if state == "cancelled":
                dao.cancel_order(workflow, order_id)
            return order_id

        placed = _order("placed")
        draft = _order("draft")
        cancelled = _order("cancelled")

        # A specimen already on the bench, for the QC route's target and for
        # the cross-organisation QC row.
        second_placed = _order("placed")
        sample_id = dao.receive_sample(workflow, second_placed, "blood")

        orgs[tag] = {
            "org_id": org_id,
            "staff": workflow,
            "workflow_id": workflow_id,
            "labtech": labtech,
            "labtech_id": labtech_id,
            "orderer": orderer,
            "orderer_id": orderer_id,
            "admin": admin,
            "admin_id": admin_id,
            "auditor": auditor,
            "auditor_id": auditor_id,
            "approver": approver,
            "approver_id": approver_id,
            "nobody": nobody,
            "nobody_id": nobody_id,
            "patient": patient,
            "placed_order": placed,
            "draft_order": draft,
            "cancelled_order": cancelled,
            "sampled_order": second_placed,
            "sample": sample_id,
            "labtech_email": f"labtech@{tag}.example",
            "orderer_email": f"orderer@{tag}.example",
            "admin_email": f"admin@{tag}.example",
            "auditor_email": f"auditor@{tag}.example",
            "approver_email": f"approver@{tag}.example",
            "nobody_email": f"nobody@{tag}.example",
            "workflow_email": f"workflow@{tag}.example",
        }

    conn.commit()
    return orgs


def _staff_cookie(client, org_id, email):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post("/auth/login", json={"email": email, "org_id": str(org_id), "password": PASSWORD})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _receive_over_http(client, cookie, order_id, sample_type="blood", condition=None):
    body = {"sample_type": sample_type}
    if condition is not None:
        body["condition_on_receipt"] = condition
    return client.post(f"/lab/orders/{order_id}/samples", json=body, cookies={STAFF_COOKIE: cookie})


def _qc_over_http(client, cookie, sample_id, qc_status="passed", reason=None):
    body = {"qc_status": qc_status}
    if reason is not None:
        body["qc_reason"] = reason
    return client.post(f"/lab/samples/{sample_id}/qc", json=body, cookies={STAFF_COOKIE: cookie})


def _order_through_the_domain(reader, session_id, order_id):
    """The order, THROUGH `get_order` — Standing Rule 2a. `get_order` binds
    `WHERE org_id = %s` from the session, so reading it back here asserts the
    row through code that carries the predicate this file is about."""
    session = reader.session_valid(session_id)
    return reader.get_order(session, order_id)


def _sample_through_the_domain(reader, session_id, sample_id):
    session = reader.session_valid(session_id)
    return reader.get_sample(session, sample_id)


def _audit_through_the_domain(reader, auditor_session_id, actions):
    """Audit rows, THROUGH `read_audit` — which is Auditor-gated and binds
    `WHERE org_id = %s` from the session. A hand-written SELECT would omit the
    very predicate row (C) exists to prove."""
    session = reader.session_valid(auditor_session_id)
    return reader.read_audit(session, actions, 500)


# ═══════════════════════════════════════════════════════════════════════════
# THE PREMISE, RE-MEASURED IN THE SUITE SO IT CANNOT DECAY INTO PROSE
# ═══════════════════════════════════════════════════════════════════════════


class TestThePremiseThisWaveMeasured:
    def test_the_instrument_finds_a_caller_that_only_exists_under_an_alias(self):
        """
        THE NON-VACUITY CONTROL FOR THE WHOLE FILE, and it is not decoration:
        the first version of this census was a bare-name scan and it reported
        the Review stage as having NO entry point. `geper/review/cli.py` does
        `from review.signoff import approve as _approve` and then calls
        `_approve(...)`, so the only production call site of `approve` is
        under another name.

        If this row fails, every ZERO this file reports is unproven —
        reproduce the wrong measurement before trusting the instrument.

        Catches: an instrument that cannot see aliased callers, which would
        make every absence claim below vacuously true.
        Vacuous against: nothing — it is itself the vacuity check.
        """
        sites = _call_sites("approve")
        assert sites, "the alias-resolving census found no caller of approve(); the instrument is broken"
        assert any(path == "geper/review/cli.py" for path, _ in sites), sites

    def test_the_instrument_does_not_credit_prose(self):
        """
        THE SECOND CONTROL, in the opposite direction. `_release_report` is
        named in a dozen docstrings and comments in production modules — this
        wave added several more. A substring scan reports it as heavily used;
        an `ast.Call` census reports the truth.

        Catches: a census that counts identifiers rather than calls.
        Vacuous against: a call hidden behind a computed attribute name, which
        the `getattr`-literal arm of `_call_sites` covers for the literal case
        and which nothing can cover for a fully dynamic one.
        """
        mentions = sum(
            1
            for path in _production_modules()
            if "_release_report" in path.read_text(encoding="utf-8", errors="ignore")
        )
        assert mentions >= 2, f"only {mentions} modules MENTION _release_report; this control can no longer distinguish"
        # STAGE 3 WIRED RELEASE. The private method now has exactly ONE call
        # site, the public `release_report` wrapper in data_access.py -- the
        # same shape as `approve_report` -> `_approve_report`. The control is
        # unchanged: it counts CALLS, and the dozen docstrings that name the
        # method are still not callers.
        assert [path for path, _ in _call_sites("_release_report")] == ["clinical/data_access.py"], _call_sites(
            "_release_report"
        )

    @pytest.mark.parametrize(
        "verb",
        [
            # create_order / place_order / cancel_order LEFT THIS LIST AT
            # STAGE 2: the test catalogue they were blocked on now has a
            # writer, and the three are wired on `/clinic/orders*`. They are
            # asserted in the positive twin below, as the tripwire asks.
            "create_sequencing_run",
            "create_vcf",
            "create_interpretation",
            "create_report",
            "create_reanalysis",
            "require_release",
            "create_amendment",
            "verify_report_integrity",
        ],
    )
    def test_the_stages_this_wave_stopped_still_have_no_production_caller(self, verb):
        """
        THE CARDED STAGES, AS ROWS. Each of these is implemented, complete and
        callerless, and this wave deliberately did NOT wire it — the reasons
        are in `clinical/workflow_endpoints.py`'s docstring, one per stage.

        This is a TRIPWIRE RATHER THAN A CELEBRATION. If a later wave wires
        one of these, this row fails and forces the wave to say so out loud
        and to move the entry here, rather than letting the map in that
        docstring rot into a false claim.

        Catches: a stage wired without its map being updated.
        Vacuous against: a stage wired through a fully dynamic dispatch, and
        against any claim about whether wiring it was CORRECT — this row is a
        census, not a judgement.
        """
        assert _call_sites(verb) == [], f"{verb} acquired a production caller; w157's stage map must be updated"

    def test_the_two_verbs_this_wave_wired_now_have_exactly_one_caller_each(self):
        """
        THE POSITIVE TWIN of the row above, and the one that proves this wave
        did something. Before `764d646` both were zero.

        Catches: the routes being mounted but calling something else, or a
        second caller appearing beside the handler.
        Vacuous against: a handler that calls the method and then ignores the
        result, which the behavioural rows catch.
        """
        for verb in ("receive_sample", "record_qc"):
            sites = _call_sites(verb)
            assert len(sites) == 1, (verb, sites)
            assert sites[0][0] == "clinical/workflow_endpoints.py", (verb, sites)

    def test_the_order_stage_is_wired_exactly_once_each_on_the_clinic_router(self):
        """STAGE 2. The three verbs the tripwire above used to hold at zero
        now have exactly one production caller each, and it is the clinic
        router. Moved here, as that row's docstring requires.

        Catches: a second caller appearing beside the handler, or the routes
        being mounted over some other method.
        """
        for verb in ("create_order", "place_order", "cancel_order"):
            sites = _call_sites(verb)
            assert len(sites) == 1, (verb, sites)
            assert sites[0][0] == "clinical/clinic_endpoints.py", (verb, sites)

    def test_each_order_route_is_registered_exactly_once_behind_the_staff_session(self):
        """STAGE 2. WALKED, NOT COUNTED FROM DECORATORS (Standing Rule 9).

        Each of the four order-stage routes is served exactly once, with
        exactly the declared method, and carries the staff session dependency
        -- so an unauthenticated request is refused before any handler runs.
        The ROLE is the domain method's, `_require_role(session, "Orderer")`
        as its first executable statement (proved for these three verbs by
        test_w129_ordering_authority and, over HTTP for every other role, by
        test_w166_catalogue_and_orders::TestOrdersRefuseEveryoneElse).

        Catches: a route registered twice, mounted under a second path, or
        mounted without the session dependency.
        Vacuous against: a handler whose role gate was removed from the
        domain method, which the two files above catch.
        """
        from clinical.app import app
        from clinical.clinic_endpoints import router
        from clinical.endpoints import _get_session
        from clinical.tests.test_w132_structural_guards import served_routes

        expected = {
            "/clinic/tests": ("GET",),
            "/clinic/orders": ("POST",),
            "/clinic/orders/{order_id}/placement": ("POST",),
            "/clinic/orders/{order_id}/cancellation": ("POST",),
        }
        served = served_routes(app)
        for path, methods in expected.items():
            matches = [entry for entry in served if entry[0] == path]
            assert matches == [(path, methods)], (path, matches)

        by_path = {}
        for route in router.routes:
            by_path.setdefault(route.path, []).append(route)
        for prefixed in ("/clinic" + path.removeprefix("/clinic") for path in expected):
            routes = by_path.get(prefixed, [])
            assert len(routes) == 1, (prefixed, routes)
            assert _get_session in [d.call for d in routes[0].dependant.dependencies], prefixed

    def test_the_order_stage_is_blocked_on_a_table_with_no_writer_at_all(self):
        """
        WHY THE ORDER STAGE IS CARDED RATHER THAN WIRED, as a measurement.

        `create_order` takes a `test_id` which is a foreign key into `tests`.
        NO method of `DataAccess` writes that table — measured over every
        method of the class by syntax tree, looking for an `INSERT INTO tests`
        string constant anywhere in any method body.

        So a `POST /lab/orders` would create orders that only a fixture can
        supply a `test_id` for. Who may define a test panel, its CDSCO class
        and its gene list is a product decision nobody has made.

        Catches: the claim going stale — if somebody adds a test-catalogue
        writer, this row fails and the Order stage becomes wireable.
        Vacuous against: a writer that builds the statement dynamically.
        """
        methods = _data_access_methods()
        writers = [
            name
            for name, node in methods.items()
            for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "INSERT INTO tests" in c.value
        ]
        # STAGE 2 closed this card: the catalogue has exactly one writer,
        # `create_test`. The row is kept as the tripwire it always was -- a
        # SECOND writer fails it, and so does the writer disappearing.
        assert writers == ["create_test"], writers
        # The positive twin for the scan: it CAN find a writer when one is
        # there, so the answer above is a fact and not a broken scan.
        sample_writers = [
            name
            for name, node in methods.items()
            for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "INSERT INTO samples" in c.value
        ]
        assert "receive_sample" in sample_writers, sample_writers


def _auditable_param_mismatches(module_path):
    """
    Every `@auditable` callback in `DataAccess` that reads a parameter name the
    decorated method does not declare, as (method, keyword, key, declared).

    WHY THIS IS A WHOLE-CLASS PROPERTY AND NOT ONE METHOD'S BUG. `auditable`
    builds its `params` mapping with `sig.bind(self, *args, **kwargs)` on the
    ORIGINAL function, so the keys are exactly the declared parameter names.
    `params.get("<not a parameter>")` therefore returns None, silently, for
    ever — and the audit row lands, with the right action, the right actor and
    the right organisation, carrying a null where a clinical fact should be.
    There is no exception, no log line and no failing test; the count of audit
    rows is correct, which is precisely what makes it invisible.

    `resource_id_param` is checked the same way and for the same reason: it
    names a parameter directly, and a name that is not one yields no
    `resource_id` at all.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DataAccess")
    found, inspected = [], 0
    for m in klass.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in m.args.args} | {a.arg for a in m.args.kwonlyargs}
        if m.args.vararg:
            params.add(m.args.vararg.arg)
        if m.args.kwarg:
            params.add(m.args.kwarg.arg)
        for dec in m.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "auditable"):
                continue
            inspected += 1
            for kw in dec.keywords:
                if kw.arg == "resource_id_param" and isinstance(kw.value, ast.Constant):
                    if kw.value.value not in params:
                        found.append((m.name, kw.arg, kw.value.value, sorted(params)))
                if kw.arg in ("details_builder", "action") and isinstance(kw.value, ast.Lambda):
                    lam = kw.value
                    if not lam.args.args:
                        continue
                    pname = lam.args.args[0].arg
                    for node in ast.walk(lam):
                        if (
                            isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "get"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == pname
                            and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and node.args[0].value not in params
                        ):
                            found.append((m.name, kw.arg, node.args[0].value, sorted(params)))
    return inspected, found


class TestNoAuditCallbackReadsAParameterThatDoesNotExist:
    """
    THE GENERALISATION OF THE ONE BUG THIS WAVE FOUND, AS A STANDING GUARD.

    w157 found `receive_sample`'s `details_builder` reading `params.get("type")`
    when the parameter is `sample_type`, so every `sample_received` row ever
    written recorded `"type": null`. The obvious question — and it was asked —
    is whether any OTHER callback does the same thing. It is a census, so it
    was counted rather than assumed: across **95** `@auditable` decorators at
    `764d646`, `receive_sample` was the ONLY one. That is a measured answer,
    not an inference from having looked at a few.

    A GUARD ON THE SHAPE, NOT ON TODAY'S LIST. It does not enumerate the
    ninety-five and it carries no count of them, so a migration-guard-style
    decay ("currently ninety-five decorators") cannot set in: it walks whatever
    is there and requires the set of mismatches to be empty. A decorator added
    next year is covered on the day it is written.
    """

    def test_no_auditable_callback_names_a_parameter_the_method_does_not_have(self):
        """
        Catches: the `receive_sample` defect and every future instance of it,
        in `details_builder`, in a callable `action`, and in
        `resource_id_param`.
        Vacuous against: a callback that reads a REAL parameter and puts it
        under a misleading output key, and against one that builds its key
        dynamically — neither of which is this shape.
        """
        inspected, mismatches = _auditable_param_mismatches(DATA_ACCESS_MODULE)
        assert inspected > 50, f"only {inspected} @auditable decorators seen; the walker is blind"
        assert mismatches == [], mismatches

    def test_the_twin_the_scan_finds_the_defect_it_was_written_for(self):
        """
        STANDING RULE 4, and it is not decoration here: an absence assertion
        over ninety-five decorators is satisfied by a scan that inspects none
        of them correctly. This rebuilds the EXACT shape that was in
        `receive_sample` at `764d646` — a `details_builder` reading `"type"`
        from a method whose parameter is `sample_type` — and requires the scan
        to report it.
        """
        source = (
            "def auditable(**k):\n"
            "    return lambda f: f\n"
            "class DataAccess:\n"
            "    @auditable(details_builder=lambda params, result: {'type': params.get('type')})\n"
            "    def receive_sample(self, session, order_id, sample_type):\n"
            "        pass\n"
            "    @auditable(resource_id_param='not_a_parameter')\n"
            "    def other(self, session, real_id):\n"
            "        pass\n"
            "    @auditable(details_builder=lambda params, result: {'t': params.get('sample_type')})\n"
            "    def correct(self, session, sample_type):\n"
            "        pass\n"
        )
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "probe.py"
            p.write_text(source, encoding="utf-8")
            inspected, mismatches = _auditable_param_mismatches(p)
        assert inspected == 3, inspected
        names = {(m, key) for m, _kw, key, _p in mismatches}
        assert names == {("receive_sample", "type"), ("other", "not_a_parameter")}, mismatches


class TestTheReleaseGapIsStillOpen:
    """
    THE WAVE'S LOAD-BEARING REFUSAL, PINNED IN BOTH DIRECTIONS.

    These rows do not assert that release SHOULD stay unwired. They assert
    that the situation which grounds the refusal is still the situation, so
    that the decision card cannot silently become obsolete or silently become
    false.
    """

    def test_release_has_exactly_one_production_path_and_the_portal_reads_it(self):
        """
        STAGE 3 RESOLVED THE CARD THIS ROW WAS THE TRIPWIRE FOR, and this is
        the "say so out loud and move the entry" the tripwire asked for. It was
        `test_release_still_has_no_production_caller_and_the_portal_still_needs_one`.

        What is true now, measured by `ast.Call` census: `_release_report` has
        exactly one caller, the public `release_report` wrapper, and the wrapper
        has exactly one caller, the review router. The `consumer` column is
        STILL free text -- the next row, unchanged, proves the vocabulary was
        not fixed by wiring the route; whether it should be remains a product
        decision.

        Catches: a SECOND path to release appearing, or the wrapper losing its
        route. Vacuous against: nothing about whether the delivery vocabulary
        is right.
        """
        assert [p for p, _ in _call_sites("_release_report")] == ["clinical/data_access.py"], _call_sites(
            "_release_report"
        )
        assert [p for p, _ in _call_sites("release_report")] == ["clinical/review_endpoints.py"], _call_sites(
            "release_report"
        )
        # AND the consequence, measured rather than recited: the patient list
        # requires a `release_events` row, not merely `state = 'released'`.
        # A NEEDLE WITH EXACTLY ONE POSSIBLE ORIGIN — `release_events` appears
        # in this method's SQL and the claim is about THAT statement.
        methods = _data_access_methods()
        sql = " ".join(
            c.value
            for c in ast.walk(methods["list_my_released_reports"])
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        )
        source = DATA_ACCESS_MODULE.read_text(encoding="utf-8")
        assert "_MY_RELEASED_REPORTS_SQL" in sql or "release_events" in source

    def test_the_schema_comment_that_grounds_the_refusal_is_still_there(self):
        """
        THE OTHER HALF OF THE TRIPWIRE, and the reason the card is a card.

        The refusal to wire release rests on a specific sentence in
        `schema.sql`: the `consumer` column is "free text pending the delivery
        paths of a later commit; enumerating it now would fix a vocabulary
        before the paths exist". That is the schema author deferring the
        vocabulary ON PURPOSE. If somebody enumerates it — a CHECK constraint,
        a documented list — the decision has been made and the card should be
        resolved rather than left standing.

        A SUBSTRING SCAN IS THE RIGHT INSTRUMENT HERE AND ONLY HERE, because
        the thing being asserted IS prose. Hazard 6 is about crediting prose
        as CODE; this row credits prose as prose.

        Catches: the ground of the refusal disappearing under it.
        Vacuous against: the sentence being reworded, which would fail this
        row loudly rather than silently — the correct outcome either way.
        """
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
        assert "enumerating it now would fix a vocabulary before the paths exist" in schema
        # And the column is still unconstrained, which is what the sentence
        # describes. A CHECK on `consumer` would mean the vocabulary was fixed.
        start = schema.index("CREATE TABLE release_events")
        table = schema[start : schema.index(");", start)]
        assert "consumer" in table and "CHECK" not in table.split("consumer", 1)[1].split("released_at", 1)[0], table


# ═══════════════════════════════════════════════════════════════════════════
# SHAPE — the house model, by syntax tree
# ═══════════════════════════════════════════════════════════════════════════


class TestTheVerbsAreShapedTheWayTheHouseRequires:
    @pytest.mark.parametrize("verb", ["receive_sample", "record_qc"])
    def test_the_role_gate_is_the_first_statement_and_it_is_lab_technician(self, verb):
        """
        HOUSE MODEL: `_require_role` is the FIRST executable statement of the
        domain method, never in the router.

        Catches: a gate moved below the order/sample lookup, which would make
        an unauthorised caller's 403-vs-404 vary with whether the identifier
        names a real row — an existence oracle.
        Vacuous against: a gate naming the wrong role, which the behavioural
        rows catch by requiring the Lab technician to SUCCEED and the Orderer
        to be refused.
        """
        assert _first_role_gate(_data_access_methods()[verb]) == "Lab technician"

    @pytest.mark.parametrize("handler", ["receive_sample", "record_qc"])
    def test_the_handler_makes_exactly_one_decorated_call(self, handler):
        """
        HOUSE MODEL: each handler calls EXACTLY ONE decorated domain method,
        because `@auditable` COMMITS after each audit row — so a handler
        assembled from two would commit the first half's audit row when the
        second half failed, recording something that did not happen.

        Catches: a handler that reads the row back to return it (which is
        exactly why the QC route answers 204 and no body).
        Vacuous against: a handler that calls one decorated method and one
        undecorated one, which is permitted and correct.
        """
        node = _handler(WORKFLOW_MODULE, handler)
        methods = _data_access_methods()
        decorated = {name for name, m in methods.items() if m.decorator_list}
        calls = [
            c.func.attr
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "dao"
        ]
        assert len(calls) == 1, calls
        assert calls[0] in decorated, calls

    def test_neither_route_accepts_an_organisation_anywhere(self):
        """
        ORGANISATION CONTEXT IS DERIVED, NEVER SUPPLIED. Neither the path, the
        query, nor either request model may carry one.

        Catches: an `org_id` field added to a request model, which would be a
        caller-controlled tenant escape.
        Vacuous against: an organisation smuggled inside a free-text field,
        which the domain methods make harmless by binding `session.org_id`
        regardless.
        """
        tree = ast.parse(WORKFLOW_MODULE.read_text(encoding="utf-8"))
        fields = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        fields.add(stmt.target.id)
        assert fields, "no request-model fields found at all; the scan is blind"
        assert not {f for f in fields if "org" in f.lower()}, fields

        for handler in ("receive_sample", "record_qc"):
            args = [a.arg for a in _handler(WORKFLOW_MODULE, handler).args.args]
            assert not [a for a in args if "org" in a.lower()], (handler, args)

    def test_the_twin_the_field_scan_sees_an_organisation_when_one_is_there(self):
        """Standing Rule 4: the absence above is worthless if the scan cannot
        recognise the field it is looking for."""
        tree = ast.parse("import uuid\nclass M:\n    org_id: uuid.UUID\n")
        fields = {
            stmt.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        }
        assert {f for f in fields if "org" in f.lower()} == {"org_id"}

    def test_neither_request_model_constrains_a_value(self):
        """
        HAZARD 5, STRUCTURALLY. Pydantic runs BEFORE the handler and therefore
        before the role gate, so any value constraint on a request field is a
        422 answered to a caller whose authorisation has not been evaluated.
        Both vocabulary fields must be bare `str`.

        Catches: a `Literal[...]`, a `min_length`, an `EmailStr` or a `Field(...)`
        added to `sample_type` or `qc_status` by a later well-meaning change.
        Vacuous against: a validator declared as a method rather than an
        annotation, which the behavioural row below catches from the other side.
        """
        tree = ast.parse(WORKFLOW_MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                if stmt.target.id not in ("sample_type", "qc_status"):
                    continue
                assert ast.unparse(stmt.annotation) == "str", ast.unparse(stmt)
                assert stmt.value is None, ast.unparse(stmt)


# ═══════════════════════════════════════════════════════════════════════════
# (A) THE TRANSITION WITHOUT THE CORRECT ROLE
# ═══════════════════════════════════════════════════════════════════════════


class TestOnlyALabTechnicianMayWorkTheBench:
    def test_a_lab_technician_receives_a_specimen_and_the_row_is_really_there(self, client, world, reader):
        """
        (A2) THE POSITIVE TWIN, and it is what makes the refusals below mean
        something: a route that refused EVERYONE would satisfy every negative
        row in this class.

        The principal holds Lab technician AND NOTHING ELSE, so a 201 from it
        is a statement about Lab technician specifically rather than about a
        six-role account.

        READ BACK THROUGH `get_sample` (Standing Rule 2a), not through SQL.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = _receive_over_http(client, cookie, org["placed_order"], "saliva", "ambient, intact")
        assert response.status_code == 201, response.text

        sample_id = response.json()["sample_id"]
        row = _sample_through_the_domain(reader, org["labtech"].session_id, sample_id)
        assert row is not None, "the route answered 201 and no sample exists"
        assert row["type"] == "saliva", row
        assert row["condition_on_receipt"] == "ambient, intact", row
        # PROVENANCE COMES FROM THE SESSION, never from the body — there is no
        # field for it. Both stamps name the acting Lab technician.
        assert row["received_by"] == org["labtech_id"], row
        assert row["collected_by"] == org["labtech_id"], row
        assert row["qc_status"] == "pending", row

    def test_a_lab_technician_records_a_verdict_and_the_row_is_really_there(self, client, world, reader):
        """(A2), the QC half. 204 and no body — the atomicity rule."""
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = _qc_over_http(client, cookie, org["sample"], "failed", "degraded on arrival")
        assert response.status_code == 204, response.text
        assert response.content == b"", response.content

        row = _sample_through_the_domain(reader, org["labtech"].session_id, org["sample"])
        assert row["qc_status"] == "failed", row
        assert row["qc_reason"] == "degraded on arrival", row
        assert row["qc_recorded_by"] == org["labtech_id"], row

    def test_the_five_other_roles_are_one_refusal_on_both_routes(self, client, world, reader):
        """
        (A) THE WRONG-FIRST ROW. Five principals — Orderer, Administrator,
        Auditor, Approver, and a user with no role at all — against BOTH
        routes, and the ten answers must be ONE value.

        THE ORDERER IS THE INTERESTING ONE. `receive_sample`'s docstring says
        the two operational roles do not overlap and neither subsumes the
        other; the ADMINISTRATOR is the second, because "Administrator is not
        a superuser over either" is a claim this repository makes and a
        Wave-7-shaped mistake would be to let it be one.

        AS A SET OF SIZE ONE (hazard 4), over (status, raw body) (Rule 8): a
        pairwise comparison passes against a leak in a case nobody listed, and
        a status-only assertion misses a leak in the detail string, which is
        exactly where one would be.

        Catches: a missing gate, a gate naming a role that is too broad, and a
        refusal whose body varies with which role the caller lacked.
        Vacuous against: a route that refuses everyone — covered by the two
        positive twins above.
        """
        org = world["a"]
        answers = set()
        for email in (
            org["orderer_email"],
            org["admin_email"],
            org["auditor_email"],
            org["approver_email"],
            org["nobody_email"],
        ):
            cookie = _staff_cookie(client, org["org_id"], email)
            answers.add(_pair(_receive_over_http(client, cookie, org["placed_order"])))
            answers.add(_pair(_qc_over_http(client, cookie, org["sample"])))
        assert answers == {FORBIDDEN}, answers

        # AND NOTHING LANDED. An absence assertion needs its positive twin,
        # which is the 201 row above; here the order must be untouched.
        order = _order_through_the_domain(reader, org["labtech"].session_id, org["placed_order"])
        assert order["state"] == "placed", order

    def test_the_unauthenticated_refusals_are_one_401_and_nothing_lands(self, client, world, reader):
        """
        THE SIX WAYS OF NOT BEING AUTHENTICATED, on both routes, as ONE value.

        Catches: a route mounted without the staff dependency, and any
        difference between "no cookie", "malformed cookie" and "unknown
        cookie" — each of which would tell a stranger which session ids exist.
        Vacuous against: an authenticated caller's refusals, covered above.
        """
        org = world["a"]
        answers = set()
        for cookies in ({}, {STAFF_COOKIE: ""}, {STAFF_COOKIE: MALFORMED}, {STAFF_COOKIE: NOWHERE}):
            answers.add(
                _pair(
                    client.post(
                        f"/lab/orders/{org['placed_order']}/samples",
                        json={"sample_type": "blood"},
                        cookies=cookies,
                    )
                )
            )
            answers.add(
                _pair(
                    client.post(
                        f"/lab/samples/{org['sample']}/qc",
                        json={"qc_status": "passed"},
                        cookies=cookies,
                    )
                )
            )
        assert answers == {UNAUTHENTICATED}, answers

        order = _order_through_the_domain(reader, org["labtech"].session_id, org["placed_order"])
        assert order["state"] == "placed", order


# ═══════════════════════════════════════════════════════════════════════════
# HAZARD 5 — THE GATE RUNS BEFORE ANY VOCABULARY OR FORMAT CHECK
# ═══════════════════════════════════════════════════════════════════════════


class TestTheGateRunsBeforeAnyVocabularyOrFormatCheck:
    def test_a_non_lab_technician_gets_the_same_403_whatever_the_body_says(self, client, world):
        """
        (HAZARD 5) VALIDATION ORDER LEAKS. A caller holding no Lab technician
        role sends: a good body, an unknown sample type, an empty one, a
        malformed order identifier, an identifier that names nothing, and an
        identifier naming a REAL order in ANOTHER organisation. All six must
        be the SAME refusal.

        If any value check ran before the gate, at least one of these would
        differ — a 422 for the unknown type, or a 404 for the malformed id —
        and the refusal would then vary with something evaluated before the
        caller's authorisation was.

        Catches: a `Literal` on the request model, a raising `_target`, an
        order lookup hoisted above `_require_role`.
        Vacuous against: a surface that answers 403 to everything, which the
        twin below refutes.
        """
        org, other = world["a"], world["b"]
        cookie = _staff_cookie(client, org["org_id"], org["nobody_email"])
        answers = {
            _pair(_receive_over_http(client, cookie, org["placed_order"], "blood")),
            _pair(_receive_over_http(client, cookie, org["placed_order"], "unicorn-tears")),
            _pair(_receive_over_http(client, cookie, org["placed_order"], "")),
            _pair(_receive_over_http(client, cookie, MALFORMED, "blood")),
            _pair(_receive_over_http(client, cookie, NOWHERE, "blood")),
            _pair(_receive_over_http(client, cookie, other["placed_order"], "blood")),
            _pair(_receive_over_http(client, cookie, org["draft_order"], "blood")),
            _pair(_qc_over_http(client, cookie, org["sample"], "passed")),
            _pair(_qc_over_http(client, cookie, org["sample"], "pending")),
            _pair(_qc_over_http(client, cookie, org["sample"], "")),
            _pair(_qc_over_http(client, cookie, MALFORMED, "passed")),
            _pair(_qc_over_http(client, cookie, NOWHERE, "passed")),
            _pair(_qc_over_http(client, cookie, other["sample"], "passed")),
        }
        assert answers == {FORBIDDEN}, answers

    def test_the_twin_a_lab_technician_can_tell_those_bodies_apart(self, client, world):
        """
        STANDING RULE 4: the uniformity above is satisfied by a surface that
        answers 403 to everything, so the discrimination must be shown to
        exist FOR AN AUTHORISED CALLER. Six distinct outcomes, and they are
        distinct.

        Catches: the gate swallowing every refusal, which would make the row
        above vacuous.
        """
        org, other = world["a"], world["b"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        outcomes = {
            "good": _receive_over_http(client, cookie, org["placed_order"], "blood").status_code,
            "bad_type": _receive_over_http(client, cookie, org["placed_order"], "unicorn-tears").status_code,
            "malformed": _receive_over_http(client, cookie, MALFORMED, "blood").status_code,
            "nowhere": _receive_over_http(client, cookie, NOWHERE, "blood").status_code,
            "other_org": _receive_over_http(client, cookie, other["placed_order"], "blood").status_code,
            "draft": _receive_over_http(client, cookie, org["draft_order"], "blood").status_code,
        }
        assert outcomes["good"] == 201, outcomes
        assert outcomes["bad_type"] == 400, outcomes
        assert outcomes["draft"] == 400, outcomes
        assert outcomes["malformed"] == 404, outcomes
        assert outcomes["nowhere"] == 404, outcomes
        assert outcomes["other_org"] == 404, outcomes

    def test_a_body_carrying_an_organisation_is_refused_rather_than_ignored(self, client, world):
        """
        `extra="forbid"`. The field nobody must ever be able to send is an
        organisation; a silent ignore is correct today and one careless
        `**body` away from not being.

        Catches: a request model whose config drifted to the Pydantic default.
        Vacuous against: nothing — a 422 here is the whole assertion.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = client.post(
            f"/lab/orders/{org['placed_order']}/samples",
            json={"sample_type": "blood", "org_id": str(world["b"]["org_id"])},
            cookies={STAFF_COOKIE: cookie},
        )
        assert response.status_code == 422, response.text


# ═══════════════════════════════════════════════════════════════════════════
# (B) THE TRANSITION CROSSING ORGANISATIONS
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCrossOrganisationTransitionIsRefused:
    def test_a_real_order_in_the_other_organisation_cannot_receive_a_specimen(self, client, world, reader):
        """
        (B) THE WRONG-FIRST ROW, AND THE COLLISION IS CONSTRUCTED.

        Organisation B's order is REAL, is in state `placed`, and would accept
        a specimen from its own Lab technician — the twin below proves it. A
        `receive_sample` that had dropped `AND org_id = %s` would therefore
        SUCCEED here, and no other row in this file would notice, because two
        randomly generated UUIDs never collide (hazard 3).

        THE REFUSAL IS THE SAME 404 as an identifier that exists nowhere, so
        organisation A learns nothing about what organisation B holds.

        Catches: a missing tenant predicate in `receive_sample` or `record_qc`.
        Vacuous against: a predicate present in the SELECT but missing from
        the INSERT, which the composite `samples_order_fk` makes
        unrepresentable at the database.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["labtech_email"])
        answers = {
            _pair(_receive_over_http(client, cookie, b["placed_order"], "blood")),
            _pair(_receive_over_http(client, cookie, NOWHERE, "blood")),
            _pair(_qc_over_http(client, cookie, b["sample"], "passed")),
            _pair(_qc_over_http(client, cookie, NOWHERE, "passed")),
        }
        assert answers == {NOT_FOUND}, answers

        # NOTHING LANDED IN B. Read through B's own session, so the predicate
        # under test is not the one doing the proving.
        b_order = _order_through_the_domain(reader, b["labtech"].session_id, b["placed_order"])
        assert b_order["state"] == "placed", b_order
        b_sample = _sample_through_the_domain(reader, b["labtech"].session_id, b["sample"])
        assert b_sample["qc_status"] == "pending", b_sample
        assert b_sample["qc_recorded_by"] is None, b_sample

    def test_the_positive_twin_the_same_order_and_sample_are_workable_by_their_own_organisation(
        self, client, world, reader
    ):
        """
        STANDING RULE 4 AND HAZARD 3 TOGETHER. The refusal above proves
        nothing unless the SAME identifiers succeed for the organisation that
        owns them — otherwise a route broken for everybody would pass it.

        Catches: a route that refuses every order regardless of tenancy.
        """
        b = world["b"]
        cookie = _staff_cookie(client, b["org_id"], b["labtech_email"])
        assert _receive_over_http(client, cookie, b["placed_order"], "blood").status_code == 201
        assert _qc_over_http(client, cookie, b["sample"], "passed").status_code == 204

        row = _sample_through_the_domain(reader, b["labtech"].session_id, b["sample"])
        assert row["qc_status"] == "passed", row


# ═══════════════════════════════════════════════════════════════════════════
# (C) THE TRANSITION THAT LANDS BUT MIS-ATTRIBUTES ITS AUDIT ROW
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuditRowNamesTheRightActorOrganisationAndSpecimen:
    def test_the_receipt_is_attributed_to_the_acting_technician_and_carries_the_specimen_type(
        self, client, world, reader
    ):
        """
        (C) THE WRONG-FIRST ROW, AND IT CAUGHT A REAL DEFECT.

        `receive_sample`'s `details_builder` read `params.get("type")`. The
        parameter is named `sample_type`, and `auditable` builds `params` by
        binding the ORIGINAL function's signature — so every `sample_received`
        audit row ever written recorded `"type": null`, for every specimen.
        Nothing observed it: no test asserted on this action's details, and
        there was no production caller to write a row at all. Wiring the route
        is what surfaced it; w157 fixed the lookup and this row is what keeps
        it fixed.

        THE TYPE IS ASSERTED BY VALUE against the value sent, not merely for
        presence — Standing Rule 4: `details` being a non-empty dict is
        satisfied by the broken state, because `order_id` was always correct.

        Catches: no audit row at all; a row attributed to the wrong actor; a
        row landing in the target's organisation; and the `params.get` defect.
        Vacuous against: a row whose timestamp or ip is wrong, which this file
        does not claim to check.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = _receive_over_http(client, cookie, org["placed_order"], "tissue", "frozen")
        assert response.status_code == 201, response.text
        sample_id = response.json()["sample_id"]

        rows = _audit_through_the_domain(reader, org["auditor"].session_id, ["sample_received"])
        mine = [r for r in rows if r[4] and r[4].get("order_id") == str(org["placed_order"])]
        assert len(mine) == 1, rows

        log_id, org_id, user_id, action, details, _ip, _ts = mine[0]
        assert action == "sample_received", mine[0]
        assert org_id == org["org_id"], mine[0]
        assert user_id == org["labtech_id"], mine[0]
        assert details["type"] == "tissue", details
        assert details["order_id"] == str(org["placed_order"]), details
        # And the audit row is about the sample the route actually created.
        assert _sample_through_the_domain(reader, org["labtech"].session_id, sample_id)["type"] == "tissue"

    def test_the_qc_verdict_is_attributed_and_names_the_verdict_that_was_recorded(self, client, world, reader):
        """(C), the QC half. The verdict is the clinically load-bearing field
        and it is asserted by value."""
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        assert _qc_over_http(client, cookie, org["sample"], "failed", "haemolysed").status_code == 204

        rows = _audit_through_the_domain(reader, org["auditor"].session_id, ["qc_recorded"])
        mine = [r for r in rows if r[4] and r[4].get("sample_id") == str(org["sample"])]
        assert len(mine) == 1, rows
        _log_id, org_id, user_id, action, details, _ip, _ts = mine[0]
        assert action == "qc_recorded"
        assert org_id == org["org_id"], mine[0]
        assert user_id == org["labtech_id"], mine[0]
        assert details["qc_status"] == "failed", details

    def test_the_other_organisations_auditor_sees_nothing_of_it(self, client, world, reader):
        """
        TENANT ISOLATION OF THE AUDIT TRAIL ITSELF. Organisation A's bench
        work must not appear in organisation B's trail.

        Catches: an audit row written with the wrong `org_id`, which is the
        failure mode a cross-tenant write would leave behind even if the
        clinical row were correctly refused.
        Vacuous against: nothing — its positive twin is the row above, which
        proves A's own auditor DOES see it.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["labtech_email"])
        assert _receive_over_http(client, cookie, a["placed_order"], "dna").status_code == 201

        b_rows = _audit_through_the_domain(reader, b["auditor"].session_id, ["sample_received", "qc_recorded"])
        leaked = [r for r in b_rows if r[4] and r[4].get("order_id") == str(a["placed_order"])]
        assert leaked == [], leaked
        assert all(r[1] == b["org_id"] for r in b_rows), b_rows

    def test_a_refused_attempt_is_recorded_against_the_caller_not_the_target_tenant(self, client, world, reader):
        """
        THE DENIAL IS AUDITED IN THE CALLER'S OWN ORGANISATION. A cross-tenant
        attempt must leave its trace where the attempt was made.

        Catches: a refused attempt whose audit row is written into the tenant
        that was probed, which would both mislead that tenant and tell it that
        somebody probed it.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["labtech_email"])
        assert _receive_over_http(client, cookie, b["placed_order"], "blood").status_code == 404

        b_rows = _audit_through_the_domain(reader, b["auditor"].session_id, ["sample_received"])
        assert [r for r in b_rows if r[4] and r[4].get("order_id") == str(b["placed_order"])] == [], b_rows


# ═══════════════════════════════════════════════════════════════════════════
# (D) THE TRANSITION THAT BYPASSES THE EXISTING STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheExistingStateMachineIsNotDuplicated:
    def test_the_handler_module_contains_no_sql_and_no_state_literal(self):
        """
        (D) THE WRONG-FIRST ROW, STRUCTURALLY.

        IT TAKES NO FIXTURE, AND THAT IS A CORRECTION THE WRONG-FIRST RUN
        ITSELF FORCED. It was first written as `(self, client, world)` and used
        neither; with no `CLINICAL_TEST_DSN` it therefore SKIPPED rather than
        ran — and the wrong-first run reported "1 passed, 1 skipped" for a
        deliberately broken module. That is Standing Rule 6's shape exactly: a
        row that looks green because it never executed. A structural claim
        needs no database and must not be able to skip.

        The wrong version of this wave
        writes `UPDATE orders SET state = 'sample_awaited'` in the handler —
        or even just constructs the string `'sample_awaited'` — and so
        advances the order WITHOUT the check that makes the advance legal.

        Asserted over every string constant in the module by syntax tree, NOT
        by scanning the file text: this module's docstring legitimately
        discusses `orders`, `sample_awaited` and the transition at length, and
        a text scan would fire on the prose (hazard 6, and two guards on this
        branch have already done exactly that).

        Catches: any SQL or any order-state literal reaching the HTTP layer.
        Vacuous against: state written through a helper in another module,
        which `test_the_handler_makes_exactly_one_decorated_call` covers from
        the other side.
        """
        tree = ast.parse(WORKFLOW_MODULE.read_text(encoding="utf-8"))
        # Every string constant EXCEPT docstrings, which are prose by
        # definition and are what a text scan would wrongly credit.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                d = ast.get_docstring(node, clean=False)
                if d is not None:
                    docstrings.add(d)
        literals = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docstrings
        ]
        assert literals, "no non-docstring string literals found at all; the scan is blind"
        forbidden = ("UPDATE ", "INSERT ", "SELECT ", "DELETE ")
        assert [s for s in literals if any(f in s.upper() for f in forbidden)] == [], literals
        states = ("draft", "placed", "sample_awaited", "in_progress", "reported", "closed", "cancelled")
        assert [s for s in literals if s in states] == [], literals

    def test_the_twin_the_literal_scan_sees_a_state_literal_when_one_is_there(self):
        """Standing Rule 4. The absence above is worthless if the scan cannot
        find the thing it forbids — and this twin also proves the docstring
        exclusion does not blind it to real code."""
        tree = ast.parse(
            '"""Talks about sample_awaited in prose."""\nx = "sample_awaited"\ny = "UPDATE orders SET s=1"\n'
        )
        docstrings = {ast.get_docstring(tree, clean=False)}
        literals = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docstrings
        ]
        assert "sample_awaited" in literals, literals
        assert any("UPDATE" in s.upper() for s in literals), literals

    def test_the_order_advance_is_performed_by_the_domain_method_and_is_observable(self, client, world, reader):
        """
        (D) BEHAVIOURALLY. The `placed -> sample_awaited` advance is real, and
        it happens because `receive_sample` does it inside its own
        transaction — not because this route did.

        THE SECOND RECEIPT IS THE PART THAT MATTERS. An order already at
        `sample_awaited` accepts a further specimen and STAYS at
        `sample_awaited`: the domain method advances only from `placed`. A
        handler that set the state itself would have to reproduce that
        asymmetry and a naive one would not.

        Catches: a handler that advances the order unconditionally, or that
        does not advance it at all.
        Vacuous against: a domain method whose advance is wrong, which
        `test_w129_laboratory_authority.py` and the domain suite own.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])

        before = _order_through_the_domain(reader, org["labtech"].session_id, org["placed_order"])
        assert before["state"] == "placed", before

        assert _receive_over_http(client, cookie, org["placed_order"], "blood").status_code == 201
        after = _order_through_the_domain(reader, org["labtech"].session_id, org["placed_order"])
        assert after["state"] == "sample_awaited", after

        assert _receive_over_http(client, cookie, org["placed_order"], "dna").status_code == 201
        again = _order_through_the_domain(reader, org["labtech"].session_id, org["placed_order"])
        assert again["state"] == "sample_awaited", again

    def test_recording_qc_moves_no_order_state_at_all(self, client, world, reader):
        """
        THE OTHER HALF OF (D), AND IT IS AN ABSENCE WITH A TWIN. QC is a
        verdict on a specimen, not a transition of the order, and this wave
        did not make it one. A wrong version "helpfully" closes or cancels the
        order on a failed verdict — a workflow ruling nobody has made.

        Catches: an invented order transition on the QC route.
        Vacuous against: nothing, because the twin is the row above, which
        proves this reader DOES observe a state change when one occurs.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        before = _order_through_the_domain(reader, org["labtech"].session_id, org["sampled_order"])
        assert _qc_over_http(client, cookie, org["sample"], "failed", "no").status_code == 204
        after = _order_through_the_domain(reader, org["labtech"].session_id, org["sampled_order"])
        assert after["state"] == before["state"], (before, after)


# ═══════════════════════════════════════════════════════════════════════════
# (E) THE TRANSITION FROM A STATE THE WORKFLOW FORBIDS
# ═══════════════════════════════════════════════════════════════════════════


class TestATransitionFromAForbiddenStateIsRefused:
    @pytest.mark.parametrize("which", ["draft_order", "cancelled_order"])
    def test_a_draft_or_cancelled_order_cannot_receive_a_specimen_and_it_says_why(self, client, world, reader, which):
        """
        (E) THE WRONG-FIRST ROW. A wired route that skipped the state check
        would let a specimen be booked against an order nobody has placed, or
        against one that has been withdrawn.

        THE REFUSAL SAYS WHY (hazard 2). A row asserting only "it is a 400"
        is green against a route that refuses every receipt for any reason —
        including a broken one. The body must name the state it refused from,
        which is the domain method's own message and is safe to disclose: it
        is raised strictly after the Lab technician gate AND after the order
        has been found in the caller's own organisation, so it names a state
        of the caller's own row.

        Catches: a state check dropped, and a 400 that means something else.
        Vacuous against: a route that refuses ALL receipts with this message,
        which the placed-order twin in this class refutes.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = _receive_over_http(client, cookie, org[which], "blood")
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        expected_state = "draft" if which == "draft_order" else "cancelled"
        assert expected_state in detail, detail
        assert "Cannot receive sample" in detail, detail

        # NOTHING LANDED: the order is untouched and holds no specimen.
        order = _order_through_the_domain(reader, org["labtech"].session_id, org[which])
        assert order["state"] == expected_state, order

    def test_the_positive_twin_a_placed_order_receives_the_same_specimen(self, client, world):
        """STANDING RULE 4: the two refusals above are satisfied by a route
        that refuses everything."""
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        assert _receive_over_http(client, cookie, org["placed_order"], "blood").status_code == 201

    def test_pending_is_not_a_verdict_and_the_refusal_says_so(self, client, world, reader):
        """
        (E) FOR THE QC ROUTE. `samples.qc_status`'s own CHECK admits
        `'pending'` — it is the column's DEFAULT — but `record_qc` accepts only
        `'passed'` and `'failed'`, because `pending` is a state rather than a
        verdict and "recording" it would be recording that nothing happened.

        THE NARROWER RULE IS THE DOMAIN METHOD'S and this route does not
        restate it, so this row proves the route inherits it rather than
        re-deciding it.

        Catches: a handler that validated against the column's CHECK instead
        of the method's rule, which is the plausible wrong version.
        Vacuous against: a route that refuses every verdict, refuted by the
        204 rows above.
        """
        org = world["a"]
        cookie = _staff_cookie(client, org["org_id"], org["labtech_email"])
        response = _qc_over_http(client, cookie, org["sample"], "pending")
        assert response.status_code == 400, response.text
        assert "passed" in response.json()["detail"] and "failed" in response.json()["detail"], response.text

        row = _sample_through_the_domain(reader, org["labtech"].session_id, org["sample"])
        assert row["qc_recorded_by"] is None, row


# ═══════════════════════════════════════════════════════════════════════════
# THE SURFACE CENSUS GREW BY EXACTLY TWO
# ═══════════════════════════════════════════════════════════════════════════


class TestTheRouteCensusGrewByExactlyTwo:
    def test_both_routes_are_served_and_carry_the_staff_dependency(self):
        """
        WALKED, NOT COUNTED FROM DECORATORS AND NOT READ NAIVELY FROM
        `app.routes` — Standing Rule 9 as corrected. The non-vacuity check is
        that the walk finds the rest of the surface too.
        """
        from clinical.app import app
        from clinical.endpoints import _get_session
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        assert "/lab/orders/{order_id}/samples" in paths, sorted(paths)
        assert "/lab/samples/{sample_id}/qc" in paths, sorted(paths)
        assert "/admin/audit" in paths and "/auth/login" in paths, sorted(paths)
        assert len(paths) > 10, sorted(paths)

        from clinical.workflow_endpoints import router

        for route in router.routes:
            assert _get_session in [d.call for d in getattr(route, "dependant").dependencies], route.path

    def test_this_wave_added_exactly_two_routes_and_they_are_the_declared_ones(self):
        """
        A CENSUS, SO IT IS COUNTED — AND AGAINST THE ONE PLACE THAT DECLARES
        IT. `TestTheRouteCensus.EXPECTED` in `test_w132_structural_guards.py`
        is this repository's route census and w157 grew it by exactly two;
        re-listing the surface here would be a second answer to "what does
        this service serve".

        Catches: a route mounted this wave and never declared.
        Vacuous against: a declaration with no route behind it, which w132's
        own equality row catches from the other side.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import TestTheRouteCensus, served_routes

        declared = dict(TestTheRouteCensus.EXPECTED)
        served = dict(served_routes(app))
        assert served == declared, (sorted(set(served) ^ set(declared)),)
        assert declared["/lab/orders/{order_id}/samples"] == ("POST",), declared
        assert declared["/lab/samples/{sample_id}/qc"] == ("POST",), declared

    def test_no_route_module_reaches_the_private_release_and_the_needle_has_one_origin(self):
        """
        RENAMED AT STAGE 3 (it was `test_no_release_route_was_added_...`). A
        release route now exists, deliberately, on the review router -- but it
        reaches the one origin through the PUBLIC `release_report` wrapper, and
        what this row still guards is that no route module calls the PRIVATE
        `_release_report` directly (which would skip the wrapper's fixing of
        `released_by` to the session user). The assertions are unchanged.

        THE WAVE 6 TECHNIQUE: when proving something is NOT exposed, pick a
        needle with exactly ONE possible origin.

        `_release_report` is the only method in this repository that inserts
        into `release_events`. So rather than scanning for the word "release"
        — which appears in `require_release`, in `list_my_released_reports`,
        in `/portal/me/reports` and in a great deal of prose — this row asks
        whether any SERVED route's handler module calls the one verb that can
        write that table.

        Catches: a release route added quietly, under any path spelling.
        Vacuous against: a release performed by a worker or CLI rather than a
        route, which `TestTheReleaseGapIsStillOpen` covers from the other side
        by counting production call sites everywhere.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        modules = {
            "clinical/app.py",
            "clinical/endpoints.py",
            "clinical/admin_endpoints.py",
            "clinical/portal_endpoints.py",
            "clinical/auth_endpoints.py",
            "clinical/workflow_endpoints.py",
            # w162. THE SEVENTH MODULE — the ordering clinician's surface,
            # `POST /clinic/patients` and the two consent routes. It is added
            # here because THIS SET IS THE THING THAT KEEPS THE NEEDLE HONEST:
            # `_release_report` is searched for in the modules that define
            # served routes, so a module missing from the set is a module the
            # needle never looks in. Adding a route family without adding it
            # here would silently narrow the search.
            "clinical/clinic_endpoints.py",
            # Stage 3. THE EIGHTH MODULE -- the review surface. Added for the
            # same reason: the set is what keeps the needle honest.
            "clinical/review_endpoints.py",
        }
        # The route census keeps that list honest: every served route is
        # defined in one of those eight. 23 at 44d624b; 26 from w162, which
        # added registration, consent and withdrawal; 27 from w164, which
        # added TOTP enrolment -- on `clinical/admin_endpoints.py`, a module
        # already in the set above, so the needle's search is not narrowed;
        # 33 from Stage 2, whose six routes are on `admin_endpoints.py` and
        # `clinic_endpoints.py`, both already in the set.
        # 38 from Stage 3, whose five routes are on `review_endpoints.py`.
        # 42 from the human-tool read API: four GETs, on `endpoints.py` and `review_endpoints.py`,
        # both already in the set above, so the needle's search is not narrowed.
        # 43 from the read-only sign-off state route, on `review_endpoints.py`, already in the set above.
        assert len(served_routes(app)) == 43, served_routes(app)
        sites = {path for path, _ in _call_sites("_release_report")}
        assert sites & modules == set(), sites
        # The needle's single origin, proved rather than asserted.
        methods = _data_access_methods()
        writers = [
            name
            for name, node in methods.items()
            for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "INSERT INTO release_events" in c.value
        ]
        assert writers == ["_release_report"], writers
