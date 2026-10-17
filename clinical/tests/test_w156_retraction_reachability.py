"""
clinical/tests/test_w156_retraction_reachability.py
───────────────────────────────────────────────────
WAVE 6, AND THE FIRST THING IT DID WAS TEST ITS OWN PREMISE.

The wave's text says "make the already-implemented capabilities reachable" and
"use the existing retraction domain mechanism". MEASURED AT `w126m-migrations`
tip `7bb6f8c`, by walking the syntax tree of all 299 non-test Python files in
the repository rather than by grepping (a substring scan credits prose as a
definition, and this floor has already had a guard fire on a docstring):

  * `retract_report`          — ZERO definitions in production. NOT
                                "implemented but unreachable". ABSENT.
  * exception AMENDMENT verb  — ZERO definitions. Migration `0004` landed the
                                tenant-safe composite relationship;
                                `exception_events.action` was still CHECKed to
                                exactly ('open', 'resolve', 'reopen').
                                UPDATED w161: migration `0005` widened that
                                CHECK to admit 'amend_resolution', so the verb
                                is no longer stopped on the schema — it is
                                stopped on being unwritten. STILL ZERO
                                definitions.
  * `read_audit`              — DEFINED (`clinical/data_access.py`) and
                                REACHABLE: `GET /admin/audit` calls it,
                                Auditor-gated, wired in w152 (WAVE 2).

So one of the three was "make reachable" and it was already made reachable two
waves ago; one was "write from nothing"; and one is STOPPED. Said plainly,
because the distinction decides what this file is:

  AUDIT-LOG ACCESS — REACHABLE ALREADY. Nothing was built for it. Its
  properties are proved in `test_w152_admin_surface.py`
  (`test_an_auditor_can_read_the_audit_trail`,
  `test_an_administrator_is_not_an_auditor`,
  `test_an_auditor_cannot_see_the_other_organisations_trail_and_the_row_is_planted`).
  This file adds ONE row about it and only one: the Approver — a role that
  reaches this surface for the first time this wave — is not an Auditor. A
  second copy of w152's rows would be a second answer to the same question.

  REPORT RETRACTION — WRITTEN FROM NOTHING, in this tree's own idiom, and
  made reachable at `POST /admin/reports/{report_id}/retraction`. Every
  product question it carries was already settled by something IN THIS TREE
  (rulings E1, E3, E4, E5, E6, E7, E8, recorded in
  `clinical/migrations/0003_report_retraction.sql`, `clinical/schema.sql` and
  `clinical/data_access.py`) except ONE — which role may retract — and the
  note above `retract_report` says exactly how that was answered and why it
  is the existing role model rather than a new decision.

  EXCEPTION AMENDMENT — STOPPED AND CARDED. Writing the verb requires the
  value `'amend_resolution'` in `exception_events.action`'s CHECK. Widening
  an inline CHECK is DROP CONSTRAINT + ADD CONSTRAINT — a destructive
  statement — so it is a migration, and migration `0004`'s own header records
  that this is precisely where the held branch stops being additive. This
  wave was instructed to add no schema. The relationship's behaviour is
  ALREADY fully proved by `test_w149_migration_0004_amendment.py`
  (same-org accepted, cross-org refused with SQLSTATE 23503 named,
  the two refusals asserted as a set of size one, audit attribution, and
  `test_5b_nothing_in_the_repository_writes_an_amendment`, which is a
  tripwire this wave deliberately leaves green). Nothing here duplicates it.

WHY THE VERB WAS WRITTEN RATHER THAN LIFTED. The held branch `w122-retraction`
(`a4907d3`) defines `retract_report` and was READ — `git show`, never merged,
never modified. Migration `0003` lifted its DDL MECHANICALLY under an approved
schema decision; no decision approved importing its APPLICATION code, and
application code is more decision-laden than DDL. It was therefore written
fresh against this tip, whose idiom has moved (`REPORT_NOT_FOUND`, the w129
phase-B authority rework, the `is_retracted` projection, the administrative
surface's `_target`/`_forbidden`/`_not_found` model — none of which existed at
`a4907d3`). Where the result converges with the held branch, that is the
schema and the recorded rulings forcing the shape, and it is said out loud in
the note above the method rather than left for a reader to guess.

THE TWO TRIPWIRES FIRED, AS THE BRIEF SAID THEY WOULD.
`test_w135_patient_report_access.py` and `test_w136_trends_retraction.py` each
carried a row asserting `def retract_report` is absent, and each said in its
own failure message that the verdict "needs revisiting" if it ever appeared.
Both are RE-AUTHORED INTO POSITIVE ROWS — the verb exists AND its first
statement is the Approver gate — and neither is deleted, skipped or weakened.
Both were re-authored onto the SYNTAX TREE rather than the file text, because
this wave added roughly a hundred lines of docstring naming the verb and a
substring row of the opposite polarity would have been satisfied by the prose.
The helper they share, `_first_role_gate`, lives here and has its non-vacuity
twin in w135.

WHAT "NO UNSAFE PATIENT EXPOSURE" MEANS HERE, AND IT IS PROVED RATHER THAN
ASSUMED. The approved patient decisions are unchanged: the patient surfaces
expose `is_retracted` ONLY. w135 and w136 already prove that for a retraction
ARRANGED BY DIRECT INSERT. What this file proves is the half that only exists
now: the same surfaces still expose only `is_retracted` when the row was
written by the VERB, through the HTTP ROUTE, by a real Approver, WITH a
replacement report present — the one field E4 makes optional and human
decision 1 refuses to the patient. That combination could not be constructed
before this wave, and it is the one a leak would ride in on.

THE SIX MEASURED HAZARDS, AND WHERE EACH IS ANSWERED:
  1. `@auditable` has no `functools.wraps` — signature inspection answers
     `(self, *args, **kwargs)`. Every structural claim here reads the SYNTAX
     TREE. `TestTheVerbIsShapedTheWayTheHouseRequires`.
  2. A refusal asserting only "it failed" is green against broken code — so
     every refusal row asserts the PAIR (status, raw body bytes), and the
     already-retracted row asserts the specific 400 and the ruling it names.
  3. Construct the collision — organisation B holds a REAL released report
     whose id organisation A names. Two random UUIDs never collide, so a
     missing org predicate is invisible without it.
  4. Refusal order leaks — refusals are asserted as SETS OF SIZE ONE, never
     pairwise.
  5. Validation order leaks — `TestTheGateRunsBeforeAnyVocabularyOrFormatCheck`
     sends an empty reason and a malformed replacement identifier as a
     non-Approver and requires the SAME 403 as a well-formed body.
  6. A substring scan credits prose as a caller — no claim here about a
     definition, a caller or a parameter is made with `in source`.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib

import pytest
from clinical.data_access import PORTAL_RELEASE_CONSUMER  # noqa: E402

from clinical.tests.test_w135_patient_report_access import (  # noqa: E402
    PASSWORD,
    RETRACTION_REASON_MARKER,
    T0,
    VCF_TEXT,
    _catalogue_test,
    _pipeline,
)

DSN = os.getenv("CLINICAL_TEST_DSN")

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
ADMIN_MODULE = CLINICAL / "admin_endpoints.py"
DATA_ACCESS_MODULE = CLINICAL / "data_access.py"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"

STAFF_COOKIE = "clinical_session"
PATIENT_COOKIE = "clinical_patient_session"

# A UUID that names nothing anywhere, and a string that is not a UUID at all.
NOWHERE = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"

# The three refusal bodies, IMPORTED rather than retyped, so a change to a
# constant fails these rows instead of silently rewriting what they assert.
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


def _first_role_gate(function_node):
    """
    The role named by `self._require_role(session, "<role>")` when that call
    is the function's FIRST EXECUTABLE statement, else None.

    SHARED WITH THE TWO RE-AUTHORED TRIPWIRES in
    `test_w135_patient_report_access.py` and `test_w136_trends_retraction.py`,
    which is why it lives at module scope here rather than inside a class.

    "FIRST EXECUTABLE" SKIPS A LEADING DOCSTRING AND NOTHING ELSE. The
    ordering is the property being asserted, not a style preference: a gate
    below the report lookup would make an unauthorised caller's refusal vary
    with whether the identifier names an existing row, which is an existence
    oracle for reports in this organisation and, worse, for reports in
    another.

    AST, NOT `inspect`. `@auditable` carries no `functools.wraps`, so
    `inspect.signature` on a decorated method answers `(self, *args,
    **kwargs)` and `inspect.getsource` returns the decorator's body. This
    reads the file's tree, where the method's own statements are.

    Its non-vacuity twin is
    `test_w135...::test_the_twin_the_tree_walk_sees_a_gate_that_is_not_first`,
    which proves it returns None for a gate that is second and for a method
    with no gate, and returns the role it actually finds.
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
    here log in as five principals. A 429 is not a refusal this file is
    about."""
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
    METHOD, never through a hand-written SELECT. A test that builds its
    expectation with raw SQL passes while the method that is supposed to read
    it is broken (the Phase 5d cannot-fail shape), and here it would be doubly
    wrong, because a hand-written SELECT could omit the very `org_id`
    predicate this file exists to prove.
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


@pytest.fixture()
def vcf(tmp_path):
    import hashlib

    path = tmp_path / "run.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _account(dao, conn, org_id, email, roles):
    """One account holding EXACTLY the roles named, and a live session for it.

    Roles are inserted directly because `assign_role` requires an existing
    Administrator and nothing in production makes the first one (the Wave 3
    deadlock). `test_identity.py` and w152 already break the cycle this way;
    this is the same break, not a new mechanism.
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
                    "w156 test fixture: signs, releases and retracts" if role == "Approver" else None,
                ),
            )
    conn.commit()
    return user_id, dao.login(email, org_id, PASSWORD)


@pytest.fixture()
def world(dao, conn, vcf):
    """
    TWO ORGANISATIONS, AND THE SECOND ONE IS THE WHOLE POINT OF ROW (B).

    Organisation B holds a REAL Approver and a REAL released report. Two
    randomly generated UUIDs never collide, so without a planted row a
    `retract_report` that had dropped its `org_id` predicate would pass every
    other row in this file — hazard 3, construct the collision.

    SIX DISTINCT PRINCIPALS IN EACH, and the separation is what lets this
    file ask "which role may retract" at all.

    THE RETRACTING PRINCIPAL HOLDS APPROVER AND NOTHING ELSE. That is
    deliberate and it is the row that would otherwise be silently weak: the
    `workflow` account has to hold every role to BUILD a released report
    (Orderer, Lab technician, Interpreter, Approver), so using it to retract
    would prove only "a principal holding six roles may retract" — true of a
    route gated on any one of them, and true of a route with no gate at all.
    `approver@…` holds Approver alone, so a 201 from it is a statement about
    Approver specifically. It is also why
    `TestTheApproverIsNotAnAuditor` can be written: the all-roles account
    holds Auditor and would read the audit trail legitimately.
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
        approver_id, approver = _account(dao, conn, org_id, f"approver@{tag}.example", ("Approver",))
        admin_id, admin = _account(dao, conn, org_id, f"admin@{tag}.example", ("Administrator",))
        auditor_id, auditor = _account(dao, conn, org_id, f"auditor@{tag}.example", ("Auditor",))
        orderer_id, orderer = _account(dao, conn, org_id, f"orderer@{tag}.example", ("Orderer",))
        nobody_id, nobody = _account(dao, conn, org_id, f"nobody@{tag}.example", ())

        test_id = _catalogue_test(conn, org_id)
        patient = dao.create_patient(workflow, f"Patient {tag.upper()}", dt.date(1990, 1, 1), "F")
        neighbour = dao.create_patient(workflow, f"Neighbour {tag.upper()}", dt.date(1985, 5, 5), "M")
        dao.enrol_patient_login(workflow, patient, "patient@shared.example", PASSWORD)

        org = {
            "org_id": org_id,
            "staff": workflow,
            "test_id": test_id,
            "workflow_id": workflow_id,
            "approver": approver,
            "approver_id": approver_id,
            "admin": admin,
            "admin_id": admin_id,
            "auditor": auditor,
            "auditor_id": auditor_id,
            "orderer": orderer,
            "orderer_id": orderer_id,
            "nobody": nobody,
            "nobody_id": nobody_id,
            "first_patient": patient,
            "second_patient": neighbour,
            "patient_email": "patient@shared.example",
            "workflow_email": f"workflow@{tag}.example",
            "approver_email": f"approver@{tag}.example",
            "admin_email": f"admin@{tag}.example",
            "auditor_email": f"auditor@{tag}.example",
            "orderer_email": f"orderer@{tag}.example",
            "nobody_email": f"nobody@{tag}.example",
        }

        _, released = _pipeline(dao, org, patient, vcf, "released")
        _, approved = _pipeline(dao, org, patient, vcf, "approved")
        _, draft = _pipeline(dao, org, patient, vcf, "draft")
        _, replacement = _pipeline(dao, org, patient, vcf, "released")
        org["released_report"] = released
        org["approved_report"] = approved
        org["draft_report"] = draft
        org["replacement_report"] = replacement
        orgs[tag] = org

    conn.commit()
    return orgs


def _staff_cookie(client, org_id, email):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post("/auth/login", json={"email": email, "org_id": str(org_id), "password": PASSWORD})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _patient_cookie(client, org_id, email):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post("/portal/login", json={"email": email, "org_id": str(org_id), "password": PASSWORD})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _retract_over_http(client, cookie, report_id, reason="w156 retraction", replacement=None):
    body = {"reason": reason}
    if replacement is not None:
        body["replacement_report_id"] = str(replacement)
    return client.post(
        f"/admin/reports/{report_id}/retraction",
        json=body,
        cookies={STAFF_COOKIE: cookie},
    )


def _retractions_of(reader, session_id, report_id):
    """Live retractions of one report, THROUGH THE DOMAIN READER.

    `_read_retraction_row` is the method `retract_report` itself uses to
    enforce E5, so reading it back here asserts the row through the code that
    owns it and carries its `org_id` predicate, not through a SELECT this test
    wrote.
    """
    session = reader.session_valid(session_id)
    return reader._read_retraction_row(session, report_id)


# ═══════════════════════════════════════════════════════════════════════════
# THE PREMISE, RE-MEASURED IN THE SUITE SO IT CANNOT DECAY INTO PROSE
# ═══════════════════════════════════════════════════════════════════════════


class TestThePremiseThisWaveTested:
    def test_the_audit_verb_was_already_reachable_and_the_retraction_verb_was_absent(self):
        """
        THE WAVE'S OWN PREMISE, AS A ROW. The dispatch said
        "already-implemented"; for two of the three capabilities that was
        false, and a claim like that belongs in the suite rather than in a
        report nobody re-runs.

        Catches: someone later deleting `GET /admin/audit` and assuming Wave 6
        built it; someone assuming the exception-amendment verb exists.
        Vacuous against: nothing — each clause is asserted in the polarity it
        actually has.
        """
        methods = _data_access_methods()
        assert "read_audit" in methods, "read_audit is gone — Wave 2's wiring has been undone"
        assert "retract_report" in methods, "retract_report is gone — Wave 6's verb has been undone"

        # The exception-amendment verb is STILL absent, and this wave did not
        # write one. When this was written, writing it ALSO needed
        # 'amend_resolution' in the action CHECK -- a destructive schema change
        # Wave 6 was told not to make. w161's migration 0005 has since made it,
        # so the schema half of the blockage is gone and only the verb is left.
        for absent in ("amend_exception", "amend_exception_resolution", "reopen_exception"):
            assert absent not in methods, (
                f"DataAccess.{absent} now exists — the exception-amendment card is resolved and "
                "this row needs revisiting"
            )

    def test_the_exception_amendment_is_now_stopped_on_the_VERB_and_no_longer_on_the_SCHEMA(self, conn):
        """
        REVISITED BY w161, WHICH IS WHAT THIS ROW ASKED FOR. Its previous form
        asserted the vocabulary was still three values and said, in its own
        failure message, "the exception-amendment card may be resolvable, and
        this row needs revisiting". Migration 0005 widened the CHECK to admit
        `'amend_resolution'`, so it has been revisited rather than discovered.

        WHY THE ROW SURVIVES AT ALL INSTEAD OF BEING DELETED. Its subject was
        never the vocabulary; it was WHY WAVE 6 STOPPED. That answer has
        CHANGED, and recording the change is more useful than removing the
        question:

          * THEN: stopped on the SCHEMA. No action value denoted an amendment,
            so the verb could not be written even if someone wanted to.
          * NOW: stopped on the VERB. The value exists, the tenant-safe
            relationship 0004 landed is reachable, and nothing in production
            writes either. What remains is a domain decision — the human's
            Approver-only rule — and not a migration.

        So the two halves are asserted in the polarity they actually have now:
        the vocabulary DOES contain the amendment value, and the verb is STILL
        absent. The second half is the one that must not quietly flip.

        NOT A DUPLICATE of `test_w161_migration_0005_vocabulary.py`: that file
        proves the migration is correct and safe. This row is the reachability
        ledger — what is stopped, and on what.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'exception_events'::regclass AND contype = 'c' "
                "AND pg_get_constraintdef(oid) LIKE '%%action%%'"
            )
            defs = [row[0] for row in cur.fetchall()]
        assert defs, "no CHECK on exception_events.action — this row is reading nothing"
        joined = " ".join(defs)
        for value in ("'open'", "'resolve'", "'reopen'"):
            assert value in joined, joined
        assert "amend_resolution" in joined, (
            "the action vocabulary has LOST its amendment value — migration 0005 has been undone"
        )

        # AND THE VERB IS STILL ABSENT. Without this half the row above would
        # be a celebration of a schema change and would say nothing about
        # reachability, which is this file's whole subject.
        methods = _data_access_methods()
        for absent in ("amend_exception", "amend_exception_resolution", "amend_resolution"):
            assert absent not in methods, (
                f"DataAccess.{absent} now exists — the exception-amendment card is resolved and "
                "this row needs revisiting"
            )


# ═══════════════════════════════════════════════════════════════════════════
# THE VERB'S SHAPE — every claim here reads the syntax tree (hazards 1 and 6)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheVerbIsShapedTheWayTheHouseRequires:
    def test_the_role_gate_is_the_first_statement_and_it_is_approver(self):
        """
        Catches: the gate deleted, the gate weakened to a different role, or
        the gate moved below the report lookup. The ORDER is the property:
        a gate after the lookup makes an unauthorised caller's refusal vary
        with whether the report exists, which is an existence oracle.

        Vacuous against: a gate that is first but is never reached because the
        decorator swallows it — covered behaviourally by
        `TestOnlyAnApproverMayRetract`.
        """
        assert _first_role_gate(_data_access_methods()["retract_report"]) == "Approver"

    def test_the_verb_takes_no_organisation_parameter_and_neither_does_the_route(self):
        """
        NO CALLER-CONTROLLED ORGANISATION ESCAPE, structurally. The
        organisation comes from `Session.org_id` and there is nowhere for a
        caller to put a different one.

        Catches: an `org_id` parameter added to either layer. Vacuous against:
        an organisation smuggled through some other name — which the
        behavioural cross-organisation rows cover.
        """
        verb = _data_access_methods()["retract_report"]
        names = {a.arg for a in verb.args.args} | {a.arg for a in verb.args.kwonlyargs}
        assert names == {"self", "session", "report_id", "reason", "replacement_report_id"}, names

        admin_tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(admin_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "retract_report"
        )
        handler_names = {a.arg for a in handler.args.args} | {a.arg for a in handler.args.kwonlyargs}
        assert handler_names == {"report_id", "body", "session", "dao"}, handler_names

        model = next(n for n in admin_tree.body if isinstance(n, ast.ClassDef) and n.name == "RetractionRequest")
        fields = {n.target.id for n in model.body if isinstance(n, ast.AnnAssign)}
        assert fields == {"reason", "replacement_report_id"}, fields

    def test_the_twin_the_parameter_scan_sees_an_organisation_when_one_is_there(self, tmp_path):
        """Standing Rule 4: without this, the row above is satisfied by a scan
        that reads nothing at all."""
        probe = tmp_path / "probe.py"
        probe.write_text("def retract_report(self, session, report_id, org_id):\n    pass\n", encoding="utf-8")
        node = ast.parse(probe.read_text(encoding="utf-8")).body[0]
        assert "org_id" in {a.arg for a in node.args.args}, "the parameter scan is reading nothing"

    def test_the_handler_makes_exactly_one_decorated_call(self):
        """
        THE `@auditable` COMPOSITION HAZARD: the decorator COMMITS after each
        audit row, so a handler assembled from two decorated calls would
        commit the first one's audit row when the second failed.

        This is the w152 census applied to the new handler specifically; the
        surface-wide version, which this wave GREW rather than weakened, is
        `test_w152_admin_surface.py::test_each_handler_calls_exactly_one_domain_method`.

        Vacuous against: a composition inside the DOMAIN layer — which is why
        `_read_retraction_row` and `_record_retraction_notification_debts` are
        asserted undecorated below.
        """
        admin_tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(admin_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "retract_report"
        )
        dao_calls = [
            n.func.attr
            for n in ast.walk(handler)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "dao"
        ]
        assert dao_calls == ["retract_report"], dao_calls

    def test_the_two_private_helpers_carry_no_decorator_at_all(self):
        """
        THE SAME HAZARD ONE LAYER DOWN. `retract_report` calls
        `_read_retraction_row` (for E5) and
        `_record_retraction_notification_debts` (for E6) from inside its own
        `@transactional` body. If either carried `@auditable`, the decorator
        would COMMIT mid-operation and a retraction whose notification debts
        failed would leave a committed retraction behind.

        Catches: a later editor "improving" a private helper by decorating it.
        Vacuous against: a helper that commits for some other reason — which
        `TestTheRetractionAndItsDebtsAreOneTransaction` covers behaviourally.
        """
        methods = _data_access_methods()
        for name in ("_read_retraction_row", "_record_retraction_notification_debts"):
            assert methods[name].decorator_list == [], (name, [ast.dump(d) for d in methods[name].decorator_list])
        assert len(_data_access_methods()["retract_report"].decorator_list) == 2

    def test_the_handler_reimplements_nothing(self):
        """
        Catches: the state rule, the role vocabulary or an organisation
        predicate copied into the router — two answers to one question, and
        the day they disagree the HTTP one wins silently.

        Vacuous against: a handler that calls the right method with the wrong
        arguments, which every behavioural row below covers.
        """
        admin_tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(admin_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "retract_report"
        )
        called = {
            n.func.attr for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "_require_role" not in called, called

        # No SQL. The docstring is excluded by construction — it is
        # `handler.body[0]` and is popped before the walk — so a row that
        # merely TALKS about a SELECT is not counted as one (hazard 6, in its
        # smallest form).
        statements = handler.body[1:] if isinstance(handler.body[0], ast.Expr) else handler.body
        literals = [
            n.value
            for stmt in statements
            for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        for literal in literals:
            assert "SELECT" not in literal.upper(), literal
            assert "org_id" not in literal, literal


# ═══════════════════════════════════════════════════════════════════════════
# (A) THE AUTHORISED CALL SUCCEEDS, AND THE WRONG ROLE DOES NOT
# ═══════════════════════════════════════════════════════════════════════════


class TestOnlyAnApproverMayRetract:
    def test_an_approver_retracts_over_http_and_the_row_is_really_there(self, client, world, reader):
        """
        THE POSITIVE TWIN every refusal row in this class needs, and it is
        read back through the DOMAIN METHOD (`_read_retraction_row`), never
        through a SELECT this test wrote — Standing Rule 2a.

        Catches: a route that answers 201 and writes nothing. Vacuous
        against: a route that writes into the wrong organisation — row (B).
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        before = _retractions_of(reader, a["auditor"].session_id, a["released_report"])
        assert before is None, before

        response = _retract_over_http(client, cookie, a["released_report"])
        assert response.status_code == 201, response.text
        retraction_id = response.json()["retraction_id"]

        row = _retractions_of(reader, a["auditor"].session_id, a["released_report"])
        assert row is not None, "the route answered 201 and wrote nothing"
        assert str(row[0]) == retraction_id, (row, retraction_id)
        # E1: the row records WHICH state it was retracted from, and this one
        # was released.
        assert row[3] == "released", row

    def test_the_four_non_approver_roles_are_one_refusal(self, client, world, reader):
        """
        (A) RETRACTION REACHABLE WITHOUT THE CORRECT ROLE — the wrong version
        this catches is a route with no gate, or one gated on Administrator
        because it lives under `/admin`.

        ASSERTED AS A SET OF SIZE ONE over four principals, not pairwise:
        refusal ORDER leaks, and a per-case equality passes against a leak in
        a case nobody listed. Administrator is in the set deliberately —
        `/admin` is a prefix, not a role, and the ONE plausible wrong version
        of this route is gated on Administrator.

        Vacuous against: a route that refuses everybody — the positive twin
        above is what closes that.
        """
        a = world["a"]
        observed = set()
        for email in (a["admin_email"], a["auditor_email"], a["orderer_email"], a["nobody_email"]):
            cookie = _staff_cookie(client, a["org_id"], email)
            observed.add(_pair(_retract_over_http(client, cookie, a["released_report"])))
        assert observed == {FORBIDDEN}, observed
        assert _retractions_of(reader, a["auditor"].session_id, a["released_report"]) is None

    def test_the_unauthenticated_refusal_is_the_401_and_nothing_lands(self, client, world, reader):
        """Catches: a route mounted without the staff session dependency."""
        a = world["a"]
        observed = {
            _pair(
                client.post(
                    f"/admin/reports/{a['released_report']}/retraction",
                    json={"reason": "w156"},
                )
            ),
            _pair(
                client.post(
                    f"/admin/reports/{a['released_report']}/retraction",
                    json={"reason": "w156"},
                    cookies={STAFF_COOKIE: NOWHERE},
                )
            ),
            _pair(
                client.post(
                    f"/admin/reports/{a['released_report']}/retraction",
                    json={"reason": "w156"},
                    cookies={STAFF_COOKIE: MALFORMED},
                )
            ),
        }
        assert observed == {UNAUTHENTICATED}, observed
        assert _retractions_of(reader, a["auditor"].session_id, a["released_report"]) is None

    def test_a_patient_session_is_not_a_staff_credential_on_this_route(self, client, world, reader):
        """A patient cookie presented under the STAFF cookie name must be
        indistinguishable from nonsense. Catches: a route that resolves a
        patient session as a staff principal."""
        a = world["a"]
        patient = _patient_cookie(client, a["org_id"], a["patient_email"])
        assert _pair(_retract_over_http(client, patient, a["released_report"])) == UNAUTHENTICATED
        assert _retractions_of(reader, a["auditor"].session_id, a["released_report"]) is None


class TestTheGateRunsBeforeAnyVocabularyOrFormatCheck:
    def test_a_non_approver_gets_the_same_403_whatever_the_body_says(self, client, world):
        """
        HAZARD 5: VALIDATION ORDER LEAKS. If `reason` carried a `min_length`,
        or `replacement_report_id` were declared `uuid.UUID`, Pydantic would
        answer 422 BEFORE the role gate ran — and the refusal would then vary
        with the CONTENT of the body for a caller whose authorisation had not
        been evaluated.

        Catches: exactly that — a validator added to `RetractionRequest`, or a
        state/reason check copied into the handler above the domain call.
        Vacuous against: a 403 that is correct for the wrong reason, which the
        structural rows above cover.

        The bodies below span: a valid one, an EMPTY reason (the domain's own
        ValueError, which only an Approver may see), a whitespace reason, a
        MALFORMED replacement identifier, a replacement naming nothing, and a
        report in a state that is not retractable. All six must be one answer.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["orderer_email"])
        bodies = [
            {"reason": "w156"},
            {"reason": ""},
            {"reason": "   "},
            {"reason": "w156", "replacement_report_id": MALFORMED},
            {"reason": "w156", "replacement_report_id": NOWHERE},
        ]
        observed = set()
        for body in bodies:
            observed.add(
                _pair(
                    client.post(
                        f"/admin/reports/{a['released_report']}/retraction",
                        json=body,
                        cookies={STAFF_COOKIE: cookie},
                    )
                )
            )
        # And the same body against three different report targets, including
        # one in a state that is not retractable and one that exists nowhere.
        for target in (a["draft_report"], NOWHERE, MALFORMED, world["b"]["released_report"]):
            observed.add(
                _pair(
                    client.post(
                        f"/admin/reports/{target}/retraction",
                        json={"reason": "w156"},
                        cookies={STAFF_COOKIE: cookie},
                    )
                )
            )
        assert observed == {FORBIDDEN}, observed

    def test_the_twin_an_approver_can_tell_those_bodies_apart(self, client, world):
        """
        Standing Rule 4, and the row above is VACUOUS WITHOUT IT: a route that
        answered 403 to everybody would satisfy it completely. An Approver
        sending the same six requests must get MORE THAN ONE answer — the
        gate is the only thing collapsing them.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        observed = {
            "empty reason": _pair(_retract_over_http(client, cookie, a["released_report"], reason="")),
            "draft report": _pair(_retract_over_http(client, cookie, a["draft_report"])),
            "nowhere": _pair(_retract_over_http(client, cookie, NOWHERE)),
            "valid": _pair(_retract_over_http(client, cookie, a["released_report"])),
        }
        assert len(set(observed.values())) > 1, observed
        assert observed["valid"][0] == 201, observed
        assert observed["nowhere"] == NOT_FOUND, observed
        assert observed["empty reason"][0] == 400, observed
        assert observed["draft report"][0] == 400, observed


# ═══════════════════════════════════════════════════════════════════════════
# (B) RETRACTION CROSSING ORGANISATIONS — the collision is CONSTRUCTED
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCrossOrganisationRetractionIsRefused:
    def test_a_real_report_in_the_other_organisation_cannot_be_retracted(self, client, world, reader):
        """
        (B), AND THE TARGET IS A REAL ROW. Organisation B's released report
        exists, is retractable BY B, and its id is handed to an Approver of A.
        Without the planted row a missing `org_id` predicate would be
        invisible, because two random UUIDs never collide (hazard 3).

        THE REFUSAL IS ASSERTED AS A SET OF SIZE ONE over three targets —
        malformed, nowhere, and the other tenant's real report — because if
        "no such report" differed from "that report is in another
        organisation", an Approver could enumerate another tenant's report ids
        from inside their own.

        AND THE OTHER TENANT'S ROW IS READ BACK, through B's own Auditor
        session: a refusal that nonetheless wrote something would satisfy the
        status assertion completely.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        observed = set()
        for target in (MALFORMED, NOWHERE, str(b["released_report"])):
            observed.add(_pair(_retract_over_http(client, cookie, target)))
        assert observed == {NOT_FOUND}, observed
        assert _retractions_of(reader, b["auditor"].session_id, b["released_report"]) is None, (
            "organisation A's refused call wrote a retraction into organisation B"
        )

    def test_the_positive_twin_the_same_report_is_retractable_by_its_own_organisation(self, client, world, reader):
        """
        Standing Rule 4: without this the row above passes against a route
        that refuses every report everywhere. Same report, same body, an
        Approver of B.
        """
        b = world["b"]
        cookie = _staff_cookie(client, b["org_id"], b["approver_email"])
        assert _retract_over_http(client, cookie, b["released_report"]).status_code == 201
        assert _retractions_of(reader, b["auditor"].session_id, b["released_report"]) is not None

    def test_a_replacement_in_the_other_organisation_is_refused_and_nothing_lands(self, client, world, reader):
        """
        THE SECOND IDENTIFIER CHANNEL, which a test written only against the
        path target would miss entirely. `replacement_report_id` is the one
        caller-supplied identifier in the BODY, and E4 makes it optional,
        so it is exactly the parameter a later editor would resolve without an
        organisation predicate.

        Catches: `SELECT 1 FROM reports WHERE id = %s` with no `org_id`.
        Vacuous against: a replacement that is refused for being absent rather
        than for being elsewhere — which is why the twin below retracts with
        the SAME shape of replacement from inside the right organisation.

        AND THE COMPOSITE FOREIGN KEY IS THE FLOOR BENEATH THIS. Even if the
        predicate were dropped, `fk_retraction_replacement` references
        `reports (org_id, id)`, so the row could not be written. Both are
        true and only one of them is code somebody has to remember.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(client, cookie, a["released_report"], replacement=b["released_report"])
        assert _pair(response) == NOT_FOUND, response.text
        assert _retractions_of(reader, a["auditor"].session_id, a["released_report"]) is None

    def test_the_positive_twin_a_same_organisation_replacement_is_accepted(self, client, world, reader):
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(client, cookie, a["released_report"], replacement=a["replacement_report"])
        assert response.status_code == 201, response.text
        assert _retractions_of(reader, a["auditor"].session_id, a["released_report"]) is not None


# ═══════════════════════════════════════════════════════════════════════════
# (C) THE AUDIT ROW — it must exist, and it must name the right organisation
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuditRowNamesTheRightActorOrganisationAndReport:
    def test_the_retraction_is_attributed_to_the_acting_approver_in_their_own_organisation(self, client, world, reader):
        """
        (C) A RETRACTION THAT LANDS BUT WRITES NO AUDIT ROW, or one naming the
        wrong organisation. Read back through `read_audit` — the DOMAIN
        METHOD, behind its own Auditor gate and its own `WHERE org_id = %s`
        — rather than through a SELECT this test wrote (Rule 2a).

        Catches: the `@auditable` decorator removed; `auditable=False`; an
        actor taken from the body; the row written against the target's
        tenant instead of the caller's.

        Vacuous against: an audit row that is correct but written OUTSIDE the
        operation's transaction — the atomicity class below covers that.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(client, cookie, a["released_report"])
        assert response.status_code == 201, response.text
        retraction_id = response.json()["retraction_id"]

        session = reader.session_valid(a["auditor"].session_id)
        rows = reader.read_audit(session, ["report_retracted"], 100)
        assert len(rows) == 1, rows
        _log_id, org_id, user_id, action, details, _ip, _ts = rows[0]
        assert action == "report_retracted", rows[0]
        assert org_id == a["org_id"], rows[0]
        assert user_id == a["approver_id"], rows[0]
        # THE AUDIT ROW NAMES THE REPORT; the retraction id travels in details.
        assert details["report_id"] == str(a["released_report"]), details
        assert details["retraction_id"] == retraction_id, details
        assert details["replacement_report_id"] is None, details

    def test_the_other_organisations_auditor_sees_nothing_of_it(self, client, world, reader):
        """
        THE TENANT HALF OF (C), and it is the row that catches an audit write
        that named the TARGET's organisation rather than the CALLER's. B's
        Auditor reading B's trail must find no retraction at all.

        Vacuous against: an empty audit trail everywhere — closed by the row
        above, which finds exactly one in A.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _retract_over_http(client, cookie, a["released_report"]).status_code == 201

        session = reader.session_valid(b["auditor"].session_id)
        assert reader.read_audit(session, ["report_retracted"], 100) == []

    def test_a_refused_attempt_is_recorded_against_the_caller_not_the_target_tenant(self, client, world, reader):
        """
        An Approver of A naming B's report is refused. Whatever the audit path
        records, it must not appear in B's trail — an authorisation failure in
        A that showed up in B's audit log would tell B that somebody probed
        it, and would tell A's auditor nothing.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _pair(_retract_over_http(client, cookie, b["released_report"])) == NOT_FOUND

        b_session = reader.session_valid(b["auditor"].session_id)
        assert reader.read_audit(b_session, ["report_retracted"], 100) == []


class TestTheRetractionAndItsDebtsAreOneTransaction:
    def test_a_retraction_of_a_released_report_records_one_debt_per_consumer(self, client, world, conn):
        """
        HUMAN RULING E6, behaviourally. `_pipeline` releases to exactly one
        consumer, so one debt is owed, its status is the single legal value,
        and its reason travels with it.

        RAW SQL HERE IS READING A TABLE NO DOMAIN METHOD EXPOSES — there is no
        reader for notification debts and this wave did not invent one
        (inventing a reader nobody asked for is how a workflow engine starts).
        The SUBJECT of this row is the WRITER, which was driven through the
        HTTP route, so this is not the bypass pattern: the expectation is not
        built from SQL, it is observed in SQL.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(client, cookie, a["released_report"], reason="w156-e6-debt")
        assert response.status_code == 201, response.text

        with conn.cursor() as cur:
            cur.execute(
                "SELECT consumer, status, reason_for_retraction, created_by FROM retraction_notifications "
                "WHERE org_id = %s AND retraction_id = %s",
                (a["org_id"], response.json()["retraction_id"]),
            )
            debts = cur.fetchall()
        assert len(debts) == 1, debts
        # The w135 fixture `_pipeline` releases to the patient portal (Stage 3
        # follow-up: a patient sees a report only if it was), so that is the one
        # consumer owed notice. The property -- one debt per distinct consumer --
        # is unchanged.
        assert debts[0][0] == PORTAL_RELEASE_CONSUMER, debts
        assert debts[0][1] == "notification_owed", debts
        assert debts[0][2] == "w156-e6-debt", debts
        assert debts[0][3] == a["approver_id"], debts

    def test_an_approved_but_never_released_report_retracts_with_no_debts_at_all(self, client, world, conn):
        """
        E6 SAYS THE DEBT DOES NOT BLOCK THE RETRACTION, and the sharpest form
        of that is a report with no recipients: the retraction still lands.
        E1 also says the row records WHICH state it came from, so this one
        must say 'approved' rather than 'released'.

        Catches: a writer that joins `release_events` INNER and so silently
        refuses to retract a report nobody received.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(client, cookie, a["approved_report"])
        assert response.status_code == 201, response.text

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM retraction_notifications WHERE org_id = %s AND retraction_id = %s",
                (a["org_id"], response.json()["retraction_id"]),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT retracted_from_state FROM report_retractions WHERE org_id = %s AND id = %s",
                (a["org_id"], response.json()["retraction_id"]),
            )
            assert cur.fetchone()[0] == "approved"

    def test_a_second_retraction_of_the_same_report_is_refused_and_says_why(self, client, world, conn):
        """
        HUMAN RULING E5: retraction is append-only and not reversible, and two
        retraction rows for one report cannot both be stated in one banner.

        ASSERT WHY, NOT MERELY THAT IT FAILED (hazard 2). A row asserting only
        `status_code == 400` would be green against a route that refused for
        an unrelated reason. The body must name the ruling, and the table must
        still hold exactly one row.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _retract_over_http(client, cookie, a["released_report"]).status_code == 201
        second = _retract_over_http(client, cookie, a["released_report"])
        assert second.status_code == 400, second.text
        assert "E5" in second.json()["detail"], second.text

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM report_retractions WHERE org_id = %s AND report_id = %s",
                (a["org_id"], a["released_report"]),
            )
            assert cur.fetchone()[0] == 1

    def test_a_refused_retraction_leaves_neither_a_row_nor_a_debt(self, client, world, conn):
        """
        THE BEHAVIOURAL HALF OF THE `@auditable` COMPOSITION HAZARD: whatever
        the audit path did on a refused call, no retraction and no debt exists
        anywhere afterwards, in EITHER organisation.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _pair(_retract_over_http(client, cookie, b["released_report"])) == NOT_FOUND
        assert _pair(_retract_over_http(client, cookie, a["draft_report"]))[0] == 400

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM report_retractions")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM retraction_notifications")
            assert cur.fetchone()[0] == 0

    def test_the_approved_report_row_itself_is_byte_for_byte_unchanged(self, client, world, conn):
        """
        THE DESIGN CLAIM, ASSERTED. A retraction says "do not act on this",
        not "this is not what was approved": `state`, `approver_id`,
        `approved_at` and `content_hash` must be identical before and after,
        and `verify_report_integrity` must still answer True.

        Catches: a writer that "helpfully" walked the state back — which the
        database would refuse anyway (`enforce_content_immutability`), making
        this the row that would name the real failure instead of an integrity
        error nobody can read.
        """
        a = world["a"]
        columns = "state, approver_id, approved_at, content_hash"
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM reports WHERE org_id = %s AND id = %s",
                (a["org_id"], a["released_report"]),
            )
            before = cur.fetchone()

        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _retract_over_http(client, cookie, a["released_report"]).status_code == 201

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM reports WHERE org_id = %s AND id = %s",
                (a["org_id"], a["released_report"]),
            )
            after = cur.fetchone()
        assert before == after, (before, after)


# ═══════════════════════════════════════════════════════════════════════════
# (D) THE PATIENT SURFACES STILL EXPOSE `is_retracted` AND NOTHING ELSE
# ═══════════════════════════════════════════════════════════════════════════


class TestThePatientSurfacesHoldAfterTheVerbExists:
    """
    "NO UNSAFE PATIENT EXPOSURE" MEANS PROVING THE SURFACES STILL HOLD, NOT
    ASSUMING IT.

    w135 and w136 already prove this for a retraction ARRANGED BY DIRECT
    INSERT. What could not be constructed before this wave, and is constructed
    here, is the combination a leak would actually ride in on: the row written
    BY THE VERB, through the HTTP ROUTE, by a real Approver, carrying BOTH a
    marker reason that cannot arrive by coincidence AND a real
    `replacement_report_id` — the field human decision 1 refuses to the
    patient by name and E4 makes optional, so it is absent from every
    previously constructible case.
    """

    # THE REPLACEMENT USED HERE IS THE APPROVED-BUT-NEVER-RELEASED REPORT, and
    # that choice is the row's teeth rather than an accident. A released
    # replacement is a report the patient ALREADY REACHES legitimately, so its
    # identifier appears in their own list on its own merits and a leak of it
    # would be indistinguishable from correct behaviour. An approved report
    # that was never released is an identifier the patient can reach through
    # NO surface at all — so if it turns up in a patient response, the only
    # path it can have travelled is the retraction row.
    REPLACEMENT_KEY = "approved_report"

    def _retract_with_everything(self, client, world):
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(
            client,
            cookie,
            a["released_report"],
            reason=RETRACTION_REASON_MARKER,
            replacement=a[self.REPLACEMENT_KEY],
        )
        assert response.status_code == 201, response.text
        return response.json()["retraction_id"]

    def test_the_list_and_the_single_report_carry_four_keys_and_the_flag_is_true(self, client, world):
        """
        Catches: a field added to the patient projection once real retraction
        rows exist. The key SET is asserted, so an added field fails here
        whatever it is called.

        Vacuous against: a surface that returns nothing — the flag is asserted
        True, which a broken reader cannot produce.
        """
        a = world["a"]
        self._retract_with_everything(client, world)
        patient = _patient_cookie(client, a["org_id"], a["patient_email"])

        listed = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: patient})
        assert listed.status_code == 200, listed.text
        mine = [r for r in listed.json() if r["report_id"] == str(a["released_report"])]
        assert len(mine) == 1, listed.json()
        assert set(mine[0]) == {"report_id", "order_id", "released_at", "is_retracted"}, mine[0]
        assert mine[0]["is_retracted"] is True, mine[0]

        single = client.get(f"/portal/me/reports/{a['released_report']}", cookies={PATIENT_COOKIE: patient})
        assert single.status_code == 200, single.text
        assert set(single.json()) == {"report_id", "order_id", "released_at", "is_retracted"}, single.json()
        assert single.json()["is_retracted"] is True, single.json()

    def test_no_reason_no_retractor_and_no_replacement_identifier_is_anywhere_in_any_body(self, client, world):
        """
        (D) A PATIENT SURFACE LEAKING THE REASON OR THE REPLACEMENT
        IDENTIFIER ONCE RETRACTION EXISTS — the wrong version this catches is
        a reader "improved" to return the retraction row rather than a
        boolean.

        RAW BYTES, over all three patient surfaces, for five needles: the
        marker reason, the replacement report id, the retracting user's id,
        the retraction id itself, and the from-state. Substring over bytes,
        because a leak nested three objects deep is still bytes on the wire.

        Vacuous against: a surface that returns nothing at all — the positive
        assertion that `"is_retracted":true` IS present is what closes that.
        """
        a = world["a"]
        retraction_id = self._retract_with_everything(client, world)
        patient = _patient_cookie(client, a["org_id"], a["patient_email"])

        responses = {
            "/portal/me/reports": client.get("/portal/me/reports", cookies={PATIENT_COOKIE: patient}),
            "/portal/me/reports/{id}": client.get(
                f"/portal/me/reports/{a['released_report']}", cookies={PATIENT_COOKIE: patient}
            ),
            "/portal/me/trends": client.get("/portal/me/trends", cookies={PATIENT_COOKIE: patient}),
        }
        needles = {
            "the retraction reason": RETRACTION_REASON_MARKER.encode(),
            "the replacement report id": str(a[self.REPLACEMENT_KEY]).encode(),
            "the retracting user id": str(a["approver_id"]).encode(),
            "the retraction id": retraction_id.encode(),
            "the from-state": b"retracted_from_state",
        }
        for path, response in responses.items():
            assert response.status_code == 200, (path, response.text)
            raw = response.content
            assert b'"is_retracted":true' in raw.replace(b" ", b""), (path, raw)
            for label, needle in needles.items():
                assert needle not in raw, f"{path} leaks {label}: {raw!r}"

    def test_the_twin_the_needles_are_findable_when_something_does_carry_them(self, client, world):
        """
        Standing Rule 4: without this, the row above is satisfied by needles
        that are spelled wrong. The STAFF-side answer to the same retraction —
        the 201 body — carries the retraction id, and the audit trail an
        Auditor reads carries the replacement id and the report id. If the
        needles were unfindable ANYWHERE, the absence row above would prove
        nothing.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        response = _retract_over_http(
            client,
            cookie,
            a["released_report"],
            reason=RETRACTION_REASON_MARKER,
            replacement=a[self.REPLACEMENT_KEY],
        )
        assert response.status_code == 201, response.text
        assert response.json()["retraction_id"].encode() in response.content

        audit = client.get(
            "/admin/audit",
            params={"action": "report_retracted"},
            cookies={STAFF_COOKIE: _staff_cookie(client, a["org_id"], a["auditor_email"])},
        )
        assert audit.status_code == 200, audit.text
        assert str(a[self.REPLACEMENT_KEY]).encode() in audit.content, audit.text

    def test_a_retraction_in_the_other_organisation_does_not_flag_this_patient(self, client, world):
        """
        THE TENANT AXIS OF THE FLAG, now driven by the VERB rather than by an
        INSERT. Both organisations hold a patient with the SAME email address,
        so a flag derived without an `org_id` predicate would light up the
        wrong one.

        Catches: `EXISTS (SELECT 1 FROM report_retractions rr WHERE
        rr.report_id = rep.id)` — a predicate missing its organisation.
        Vacuous against: a flag that is always False — the twin is the
        assertion that B's own patient DOES see True.
        """
        a, b = world["a"], world["b"]
        cookie = _staff_cookie(client, b["org_id"], b["approver_email"])
        assert _retract_over_http(client, cookie, b["released_report"]).status_code == 201

        a_patient = _patient_cookie(client, a["org_id"], a["patient_email"])
        a_body = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: a_patient}).json()
        assert [r["is_retracted"] for r in a_body] == [False, False], a_body

        b_patient = _patient_cookie(client, b["org_id"], b["patient_email"])
        b_body = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: b_patient}).json()
        assert sorted(r["is_retracted"] for r in b_body) == [False, True], b_body

    def test_the_retracted_report_is_still_listed_and_still_readable(self, client, world):
        """
        RULINGS E3 AND E7: retraction FLAGS and never FILTERS. A retracted
        report that vanished from the patient's own list would be the
        invisible withdrawal the whole mechanism exists to replace.

        Catches: a later implementer adding `AND NOT EXISTS (SELECT 1 FROM
        report_retractions ...)` once the verb makes such rows real — which is
        exactly the temptation this wave created.
        """
        a = world["a"]
        patient = _patient_cookie(client, a["org_id"], a["patient_email"])
        before = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: patient}).json()
        self._retract_with_everything(client, world)
        after = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: patient}).json()

        assert {r["report_id"] for r in before} == {r["report_id"] for r in after}, (before, after)
        assert str(a["released_report"]) in {r["report_id"] for r in after}
        assert (
            client.get(f"/portal/me/reports/{a['released_report']}", cookies={PATIENT_COOKIE: patient}).status_code
            == 200
        )


# ═══════════════════════════════════════════════════════════════════════════
# (F) AUDIT DISCLOSURE TO A ROLE THAT SHOULD NOT SEE IT
# ═══════════════════════════════════════════════════════════════════════════


class TestTheApproverIsNotAnAuditor:
    """
    THE ONE AUDIT ROW THIS WAVE ADDS, AND ONLY ONE.

    Audit-log access was already reachable and is already tested in
    `test_w152_admin_surface.py`: an Auditor reads, an Administrator does not,
    and a planted row in the other organisation is unreachable. Copying those
    rows here would be a second answer to the same question.

    What is NEW is that the Approver role now reaches this surface. A gate
    that had been written as "any principal on /admin" — the plausible wrong
    version of the retraction route — would also have opened the audit trail
    to the retractor.
    """

    def test_an_approver_cannot_read_the_audit_trail(self, client, world):
        """
        Catches: `read_audit`'s Auditor gate relaxed to accommodate the new
        role, or a route-level gate replacing the domain one.

        Vacuous against: a 403 for everybody — the twin below reads the trail
        as an Auditor in the same organisation, over the same route.
        """
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["approver_email"])
        assert _pair(client.get("/admin/audit", cookies={STAFF_COOKIE: cookie})) == FORBIDDEN

    def test_the_twin_an_auditor_in_the_same_organisation_reads_it(self, client, world):
        a = world["a"]
        cookie = _staff_cookie(client, a["org_id"], a["auditor_email"])
        response = client.get("/admin/audit", cookies={STAFF_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json(), "the audit trail is empty — this twin proves nothing"


# ═══════════════════════════════════════════════════════════════════════════
# THE SURFACE CENSUS GREW BY EXACTLY ONE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheRouteIsServedAndItIsTheOnlyOneAdded:
    def test_the_retraction_route_is_served_and_carries_the_staff_dependency(self):
        """
        WALKED, NOT COUNTED FROM DECORATORS AND NOT READ NAIVELY FROM
        `app.routes` — Standing Rule 9 as CORRECTED: `include_router` leaves
        an opaque wrapper, so `served_routes` descends into it. The
        non-vacuity check is that the walk finds the other administrative
        routes too.
        """
        from clinical.app import app
        from clinical.endpoints import _get_session
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        assert "/admin/reports/{report_id}/retraction" in paths, sorted(paths)
        assert "/admin/audit" in paths, sorted(paths)
        assert len(paths) > 5, sorted(paths)

        from clinical.admin_endpoints import router

        route = next(r for r in router.routes if r.path == "/admin/reports/{report_id}/retraction")
        assert _get_session in [d.call for d in getattr(route, "dependant").dependencies]

    def test_this_wave_added_exactly_one_route_and_it_is_the_declared_one(self):
        """
        A CENSUS, SO IT IS COUNTED — AND AGAINST THE ONE PLACE THAT DECLARES
        IT, not against a second list written here. `TestTheRouteCensus
        .EXPECTED` in `test_w132_structural_guards.py` is this repository's
        route census and w156 grew it by exactly one entry; re-listing the
        surface in this file would be a second answer to "what does this
        service serve", and the day the two disagreed nobody would know which
        was right.

        Catches: a route mounted this wave and never declared — the served
        surface would then contain a path the census does not. Vacuous
        against: a declaration with no route behind it, which w132's own
        equality row catches from the other side.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import TestTheRouteCensus, served_routes

        declared = dict(TestTheRouteCensus.EXPECTED)
        served = dict(served_routes(app))
        assert served == declared, (sorted(set(served) ^ set(declared)),)
        assert declared["/admin/reports/{report_id}/retraction"] == ("POST",), declared
