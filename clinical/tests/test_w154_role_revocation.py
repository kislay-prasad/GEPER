"""
clinical/tests/test_w154_role_revocation.py
────────────────────────────────────────────
w154 (WAVE 4) — STAFF AND ROLE ADMINISTRATION, and the one thing in it that
was not wiring: ROLE REVOCATION HAD NO PRODUCTION WRITER AT ALL.

THE MEASUREMENT THIS FILE IS BUILT ON, taken at 4700f40 before a line was
written, and re-taken here by the syntax tree so it is a test rather than a
memory:

  * `revoked_at` / `revoked_by` occurred at exactly TWO non-test code sites,
    `_get_actor_role` and `get_user_roles`, and BOTH were `revoked_at IS NULL`
    READ PREDICATES.
  * THREE production sites wrote a `role_assignments` row — `assign_role`,
    `provision_organisation`, `_create_system_session` — and not one ever set
    those two columns.
  * So the platform could grant a role, and could read a revocation, and had
    no way to record one. The enforcement was complete and the state it
    enforced on was unreachable.

`TestTheProductionWriterExists` re-runs that census and asserts it now finds
exactly one writer, and its twin proves the census can return zero.

──────────────────────────────────────────────────────────────────────────
THE DESIGN QUESTION, AND WHY IT IS ANSWERED BY MEASUREMENT
──────────────────────────────────────────────────────────────────────────

One question decided the whole shape of the writer: HOW ARE ROLES RESOLVED?

If authorisation reads role assignments PER REQUEST, revocation needs no
session surgery — the next request simply fails. If roles are captured into
the session at login, revocation must deal with live sessions, and
`set_user_disabled` shows the house pattern for that.

MEASURED AT 4700f40, and the answer is unambiguous:

  * `Session` is a frozen dataclass of exactly three fields — `session_id`,
    `user_id`, `org_id`. No role.
  * `session_valid` reads ONE `sessions` row and returns those three fields.
    It does not join `role_assignments`.
  * `_require_role` calls `get_user_roles`, which reads `role_assignments`
    with `revoked_at IS NULL`, on EVERY gated call.

Roles are resolved per request. Setting `revoked_at` IS the enforcement.
Terminating the target's sessions would be a SECOND mechanism for an effect
the first one already has, and two mechanisms can disagree.

That is not what `set_user_disabled` does, and the asymmetry is the whole
point rather than an inconsistency: `users.disabled` is read by NOTHING on
the authenticated path, which is why w133 had to end sessions inside that
method's body. Enforcement did not reach; here it reaches everything.

`TestRolesAreResolvedPerRequest` proves the premise behaviourally — a LIVE
session loses the capability without being touched — and
`TestNoSecondMechanismWasBuilt` proves the conclusion structurally.

──────────────────────────────────────────────────────────────────────────
THE FOUR MEASURED HAZARDS THIS FILE IS WRITTEN AGAINST
──────────────────────────────────────────────────────────────────────────

1. `@auditable` HAS NO `functools.wraps`, so `inspect.signature()` on ANY
   decorated method returns `(self, *args, **kwargs)`. Wave 3's signature
   rows passed against a wrong version outright because of it. Every claim
   here about a decorated method's parameters is read from the SYNTAX TREE,
   and `TestTheParameterReaderIsNotTheBrokenOne` proves the reader sees real,
   DIFFERING parameters on three methods rather than a constant.

2. A REFUSAL ROW ASSERTING ONLY "IT FAILED" CAN BE GREEN AGAINST BROKEN CODE.
   Every refusal here is a `(status, raw body)` pair, never a bare status and
   never a bare "it raised".

3. A MISSING PREDICATE IS INVISIBLE WHEN IDENTIFIERS CANNOT COLLIDE BY
   ACCIDENT. Two random UUIDs never collide, so the collision is CONSTRUCTED:
   `target_b` is a real user in organisation B holding a real, live Approver
   role, and organisation A names it by id.

4. REFUSAL ORDER LEAKS. Wave 2's first version answered 404 for a malformed
   id BEFORE the role gate, so a non-Administrator saw 404-vs-403 and the 403
   stopped being a function of the caller's roles alone. Every refusal claim
   here is asserted as a SET OF SIZE ONE over many inputs, never pairwise —
   and the inputs deliberately include the ones a mis-ordered implementation
   would answer differently: a malformed id, an id in another tenant, an
   unknown role name, and `System`.

──────────────────────────────────────────────────────────────────────────
THE WRONG VERSIONS, AND WHICH ROW CATCHES EACH
──────────────────────────────────────────────────────────────────────────

A. REVOCATION THAT MARKS THE ROW AND LEAVES AUTHORISATION UNAFFECTED — e.g.
   a writer keyed on one `assignment_id` while the user holds two grants of
   that role.
   CAUGHT BY: `TestRolesAreResolvedPerRequest::
   test_a_live_session_loses_the_capability_the_moment_the_role_is_revoked`
   and `::test_a_duplicate_grant_does_not_survive_the_revocation`.
   VACUOUS AGAINST: a revoker that writes nothing at all but returns 0 — the
   role is still held, so those rows fail, but they would fail identically
   for a method that raised. `TestTheAuthorisedRevocationSucceeds` is the
   positive twin that distinguishes them.

B. REVOCATION THAT SUCCEEDS ACROSS ORGANISATIONS.
   CAUGHT BY: `TestTheCrossOrganisationRevocationIsRefused`, and ONLY because
   `target_b` really holds a live Approver role in B. Without that planted
   row every assertion in the class would pass against a missing `org_id`
   predicate.
   VACUOUS AGAINST: a revoker that refuses everything. The positive twin in
   the same class is what makes the refusal mean something.

C. A DISABLED ADMINISTRATOR STILL ABLE TO ACT.
   CAUGHT BY: `TestTheDisabledAdministratorCannotContinue::
   test_the_disabled_administrators_live_session_cannot_revoke_anything`,
   whose positive twin is the SAME session succeeding one request earlier.
   VACUOUS AGAINST: a revoke route that refuses every caller — again the
   positive twin is what rules that out.

D. THE AUDIT ROW NAMING THE WRONG ORGANISATION.
   THE CORRECT ANSWER, AND WHY. On a SUCCESSFUL revocation the actor's
   organisation and the target's are necessarily the same value: `org_id` is
   bound from the session, `get_user` refuses a target outside it, and the
   composite foreign key on `(user_id, org_id)` refuses the row regardless.
   They cannot differ, so "which one" is only a real question on a REFUSED
   cross-organisation attempt — and there the row belongs to the ACTOR's
   organisation. Filing it under the target's would write a row into another
   tenant's audit trail on the strength of a caller who has no standing in
   it, and organisation B's Auditor would read an event about their own user
   caused by a principal they cannot see.
   CAUGHT BY: `TestTheAuditRowNamesTheActorTheOrganisationAndTheTarget::
   test_a_refused_cross_organisation_attempt_is_filed_under_the_callers_own_org`.
   VACUOUS AGAINST: a version that writes no audit row at all for a refusal.
   `::test_the_refusal_is_recorded_at_all` is the twin for exactly that.

E. AN ADMINISTRATOR REVOKING THEIR OWN LAST ADMINISTRATOR ROLE.
   THE EXISTING RULES DO NOT SPEAK TO IT, AND THAT IS THE FINDING. There is
   no self-targeting rule anywhere in `DataAccess`: `set_user_disabled` lets
   an Administrator disable their own account and its handler says so in
   terms ("preserving an existing rule includes preserving its absence"), and
   nothing guards a "last Administrator". So the preserved behaviour is that
   it SUCCEEDS and the caller is refused on their next administrative
   request. Inventing a guard here would be widening an existing rule under
   cover of preserving it.
   CAUGHT BY: `TestTheExistingRulesArePreserved::
   test_an_administrator_may_revoke_their_own_role_and_is_refused_immediately_after`
   (which fails against a version that invents the guard) and
   `::test_structurally_the_writer_compares_no_identifier_to_the_callers_own`
   (which fails against a guard added silently later).
   VACUOUS AGAINST: nothing in the self-targeting family; it is vacuous
   against a revoker that is broken for every caller, which the positive
   twins elsewhere exclude.
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

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
DATA_ACCESS_MODULE = CLINICAL / "data_access.py"
ADMIN_MODULE = CLINICAL / "admin_endpoints.py"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
STAFF_PASSWORD = "correct horse battery staple"

STAFF_COOKIE = "clinical_session"

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND  # noqa: E402
from clinical.data_access import GENERIC_AUTH_ERROR, HUMAN_ASSIGNABLE_ROLES, ROLES  # noqa: E402


def _body(detail):
    """The exact bytes FastAPI serialises `{"detail": ...}` to.

    RAW BYTES rather than a parsed dict, for `test_w152_admin_surface.py`'s
    reason: what must be indistinguishable is what goes on the wire, and a
    dict is not hashable so a refusal SET could not be formed from it.
    """
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


UNAUTHENTICATED = (401, _body(GENERIC_AUTH_ERROR))
FORBIDDEN = (403, _body(ADMIN_FORBIDDEN))
NOT_FOUND = (404, _body(ADMIN_NOT_FOUND))

NOWHERE = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"


def _pair(response):
    """(status, raw body bytes) — BOTH CHANNELS, as one hashable value.

    Standing Rule 8: the body alone misses the w132 status-channel oracle and
    the status alone misses the w129b body-channel one.
    """
    return (response.status_code, response.content)


# ═══════════════════════════════════════════════════════════════════════════
# THE SYNTAX-TREE READERS — hazard 1, and every structural claim below
# ═══════════════════════════════════════════════════════════════════════════


def _data_access_methods():
    tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
    out = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = node
    return out


def _ast_params(method_name):
    """The parameter names a method REALLY declares, read from the source.

    THE ONLY READER THIS FILE USES, and the reason is measured rather than
    stylistic: `@auditable`'s wrapper is a plain `def wrapper(self, *args,
    **kwargs)` with NO `functools.wraps`, so `inspect.signature` on any
    decorated `DataAccess` method reports `(self, *args, **kwargs)` and an
    assertion built on it is true of every method in the class at once.
    """
    node = _data_access_methods()[method_name]
    return [a.arg for a in node.args.args]


def _body_strings(node):
    """Every string constant inside a function, EXCLUDING its own docstring.

    Docstrings are excluded by IDENTITY, not by text: these methods' prose
    quotes the very statements being looked for, and a text-shaped exclusion
    would either swallow a real offender or trip over the explanation.
    """
    doc = None
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
        doc = id(node.body[0].value)
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) != doc
    ]


def _production_files_mentioning(needle):
    """Non-test production .py files under clinical/ that contain `needle`.

    SCOPED AND PROVED NON-EMPTY by every caller. `role_assignments` exists
    nowhere else in the tree — the guard for that claim is
    `test_the_revocation_columns_are_a_clinical_concern_only`.
    """
    found = []
    for path in sorted(CLINICAL.rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if needle in text:
            found.append(path)
    return found


def _revocation_writers():
    """Every production site that WRITES `revoked_at`, by syntax tree.

    A WRITE is a string constant, inside a function body and not its
    docstring, that contains both `revoked_at =` and `revoked_by`. A
    `revoked_at IS NULL` read predicate matches neither, which is the whole
    distinction the pre-wave census turned on.
    """
    writers = []
    for path in _production_files_mentioning("revoked_at"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for value in _body_strings(node):
                upper = value.upper()
                if "REVOKED_AT =" in upper and "REVOKED_BY" in upper:
                    writers.append((path.name, node.name))
    return sorted(set(writers))


def _revocation_readers():
    """Every production site that READS `revoked_at IS NULL` AND DOES NOT WRITE.

    The `AND NOT a write` half is load-bearing rather than tidy: the writer's
    own statement carries `WHERE ... revoked_at IS NULL`, so that clause alone
    would count it as a third reader and the census would stop distinguishing
    the two kinds of site — which is the exact distinction this wave turns on.
    """
    readers = []
    for path in _production_files_mentioning("revoked_at"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for value in _body_strings(node):
                upper = value.upper()
                if "REVOKED_AT IS NULL" in upper and "REVOKED_AT =" not in upper:
                    readers.append((path.name, node.name))
    return sorted(set(readers))


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

    Inserted directly, the way `test_identity.py::admin_session` and
    `test_w152_admin_surface.py::_grant` already do. `provision_organisation`
    (Wave 3) is the real bootstrap and it is exercised by its own suite; here
    the point is a controlled starting set of roles, not the bootstrap.
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
    TWO ORGANISATIONS, AND THE SECOND ONE HOLDS A REAL LIVE ROLE.

    `target_b` is a real user in organisation B with a real, unrevoked
    Approver assignment. Organisation A will name that user's id. Two random
    UUIDs never collide, so WITHOUT this planted row a revoker that dropped
    its `org_id` predicate would pass every other row in this file — the
    cross-organisation class would be asserting a 404 that a missing
    predicate also produces, because there would be nothing to find.

    Organisation A holds TWO Administrators on purpose: `admin_a` acts, and
    `admin2_a` is the one that gets disabled and the one whose own role is
    revoked, so neither experiment destroys the actor.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))

    org_a = dao.create_organisation("Org A")
    org_b = dao.create_organisation("Org B")

    admin_a = dao.create_user(org_a, "admin@a.example", STAFF_PASSWORD)
    admin2_a = dao.create_user(org_a, "admin2@a.example", STAFF_PASSWORD)
    auditor_a = dao.create_user(org_a, "auditor@a.example", STAFF_PASSWORD)
    orderer_a = dao.create_user(org_a, "orderer@a.example", STAFF_PASSWORD)
    target_a = dao.create_user(org_a, "target@a.example", STAFF_PASSWORD)

    admin_b = dao.create_user(org_b, "admin@b.example", STAFF_PASSWORD)
    auditor_b = dao.create_user(org_b, "auditor@b.example", STAFF_PASSWORD)
    target_b = dao.create_user(org_b, "target@b.example", STAFF_PASSWORD)

    _grant(connection, admin_a, org_a, ("Administrator",))
    _grant(connection, admin2_a, org_a, ("Administrator",))
    _grant(connection, auditor_a, org_a, ("Auditor",))
    _grant(connection, orderer_a, org_a, ("Orderer",))
    # The target in A holds TWO roles, so "revoking one leaves the other" is
    # a property this file can assert rather than assume.
    _grant(connection, target_a, org_a, ("Approver", "Orderer"))
    _grant(connection, admin_b, org_b, ("Administrator",))
    _grant(connection, auditor_b, org_b, ("Auditor",))
    # THE CONSTRUCTED COLLISION. A real, live role in the other tenant.
    _grant(connection, target_b, org_b, ("Approver",))
    connection.commit()

    admin_a_session = dao.login("admin@a.example", org_a, STAFF_PASSWORD)
    admin2_a_session = dao.login("admin2@a.example", org_a, STAFF_PASSWORD)
    auditor_a_session = dao.login("auditor@a.example", org_a, STAFF_PASSWORD)
    orderer_a_session = dao.login("orderer@a.example", org_a, STAFF_PASSWORD)
    admin_b_session = dao.login("admin@b.example", org_b, STAFF_PASSWORD)
    auditor_b_session = dao.login("auditor@b.example", org_b, STAFF_PASSWORD)
    target_a_session = dao.login("target@a.example", org_a, STAFF_PASSWORD)

    connection.commit()
    connection.close()

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_a": admin_a,
        "admin2_a": admin2_a,
        "auditor_a": auditor_a,
        "orderer_a": orderer_a,
        "target_a": target_a,
        "admin_b": admin_b,
        "auditor_b": auditor_b,
        "target_b": target_b,
        "admin_a_sid": str(admin_a_session.session_id),
        "admin2_a_sid": str(admin2_a_session.session_id),
        "auditor_a_sid": str(auditor_a_session.session_id),
        "orderer_a_sid": str(orderer_a_session.session_id),
        "admin_b_sid": str(admin_b_session.session_id),
        "auditor_b_sid": str(auditor_b_session.session_id),
        "target_a_sid": str(target_a_session.session_id),
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

    STANDING RULE 2a: every assertion about who holds what goes through the
    DOMAIN METHOD (`session_valid`, `get_user_roles`, `get_user`), never
    through hand-written SQL. A test that builds its expectation from raw SQL
    passes while the method that is supposed to read it is broken — and here
    it would be doubly wrong, because a hand-written SELECT could omit the
    `revoked_at IS NULL` predicate this whole wave turns on.
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
    yield dao
    connection.close()


@pytest.fixture()
def raw(clinical_dsn):
    """A RAW CONNECTION, used for exactly one thing.

    `revoked_by` has no domain reader — `RoleAssignment` deliberately does not
    carry it — so proving the writer filled BOTH columns needs direct SQL.
    That is an ATTRIBUTION check on a column no code reads, NOT an
    access-rule check: every claim about who may do what goes through the
    domain reader above. Rule 2a forbids building an access expectation from
    raw SQL; it does not forbid inspecting a column nothing exposes.
    """
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


def _roles_of(reader, session_id, user_id):
    """The active roles of a user, THROUGH THE DOMAIN READER, via a session
    re-derived exactly as `_get_session` derives it."""
    session = reader.session_valid(session_id)
    return sorted(a.role for a in reader.get_user_roles(session, user_id))


def _revoke(client, sid, user_id, role):
    return client.delete(f"/admin/users/{user_id}/roles/{role}", cookies={STAFF_COOKIE: sid})


# ═══════════════════════════════════════════════════════════════════════════
# THE GAP THIS WAVE EXISTS FOR — required test: "revocation has an actual
# production writer"
# ═══════════════════════════════════════════════════════════════════════════


class TestTheProductionWriterExists:
    def test_the_revocation_columns_now_have_exactly_one_production_writer(self):
        """
        THE CENSUS, RE-RUN. Before this wave it returned ZERO.
        """
        assert _revocation_writers() == [("data_access.py", "revoke_role")], _revocation_writers()

    def test_the_readers_the_census_originally_found_are_still_the_only_readers(self):
        """
        The other half of the same census, and it is the reason the writer
        needs no session surgery: the two sites that enforce on this state
        are the two that always did, and both are per-request reads.
        """
        # Milestone 2, Phase A added two DISPLAY-ONLY readers of the same predicate (`revoked_at IS NULL`): the
        # staff directory shows a user's CURRENT roles (`_staff_directory_roles`) and filters on a currently held
        # role (`list_staff_users`). Neither enforces anything -- the two sites that ENFORCE on this state are
        # still the two that always did, both per-request reads -- so the census grows by exactly those two.
        assert _revocation_readers() == [
            ("data_access.py", "_get_actor_role"),
            ("data_access.py", "_staff_directory_roles"),
            ("data_access.py", "get_user_roles"),
            ("data_access.py", "list_staff_users"),
        ], _revocation_readers()

    def test_the_twin_the_census_returns_nothing_for_a_module_that_only_reads(self):
        """
        STANDING RULE 4. `test_..._exactly_one_production_writer` is an
        equality against a one-element list and would be satisfied by a
        scanner that matched the word `revoke` anywhere. This proves the
        scanner distinguishes a WRITE from the `revoked_at IS NULL` READ
        PREDICATE that was all the tree contained before this wave — which is
        the exact discrimination the pre-wave measurement turned on.
        """
        module = ast.parse(
            "class D:\n"
            "    def read_one(self):\n"
            '        """Prose that mentions revoked_at = now and revoked_by, and explains it."""\n'
            '        return self._query("SELECT role FROM role_assignments WHERE revoked_at IS NULL")\n'
        )
        writes, reads = [], []
        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for value in _body_strings(node):
                    upper = value.upper()
                    if "REVOKED_AT =" in upper and "REVOKED_BY" in upper:
                        writes.append(node.name)
                    if "REVOKED_AT IS NULL" in upper:
                        reads.append(node.name)
        assert writes == [], writes
        assert reads == ["read_one"], reads

    def test_the_route_is_served_and_is_the_inverse_of_the_grant_route(self):
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        served = dict(served_routes(app))
        assert "/admin/users/{user_id}/roles/{role}" in served, sorted(served)
        assert "DELETE" in served["/admin/users/{user_id}/roles/{role}"], served
        # The grant route it inverts, so the pair is asserted together.
        assert "POST" in served["/admin/users/{user_id}/roles"], served

    def test_the_route_carries_the_shared_staff_session_dependency(self):
        """A new administrative route without it would be an unauthenticated
        administrative route, and nothing else in this repository would
        notice. Enumerated from the router, not read from the source."""
        from clinical.admin_endpoints import router
        from clinical.endpoints import _get_session

        matched = [r for r in router.routes if getattr(r, "path", None) == "/admin/users/{user_id}/roles/{role}"]
        assert matched, [getattr(r, "path", None) for r in router.routes]
        for route in matched:
            assert _get_session in [d.call for d in route.dependant.dependencies], route.path


class TestTheParameterReaderIsNotTheBrokenOne:
    """HAZARD 1, made into a test rather than a memory."""

    def test_the_syntax_tree_reader_sees_the_real_parameters_of_three_methods(self):
        """
        THE NON-VACUITY TWIN FOR EVERY STRUCTURAL CLAIM IN THIS FILE.

        Three methods, three DIFFERENT answers. A reader that returned a
        constant — which is exactly what `inspect.signature` does here —
        cannot produce three different lists.
        """
        assert _ast_params("revoke_role") == ["self", "session", "target_user_id", "role"]
        assert _ast_params("assign_role") == ["self", "session", "target_user_id", "role", "basis"]
        assert _ast_params("read_audit") == ["self", "session", "actions", "limit"]

    def test_the_naive_reader_cannot_see_them_which_is_why_it_is_not_used(self):
        """
        `@auditable`'s wrapper is `def wrapper(self, *args, **kwargs)` with no
        `functools.wraps`, so `inspect.signature` reports the WRAPPER's
        parameters for every decorated method in the class. Wave 3's
        signature rows passed against a wrong version because of it.

        Written so that FIXING the decorator does not fail this row — the
        claim is "the naive reader is either broken or agrees", and the row
        that does the work is the one above.
        """
        from clinical.data_access import DataAccess

        naive = [p.name for p in inspect.signature(DataAccess.revoke_role).parameters.values()]
        real = _ast_params("revoke_role")
        assert naive in (["self", "args", "kwargs"], real), (
            f"inspect.signature reports {naive}; the syntax tree reports {real}. "
            "Neither the broken form nor agreement — the reader has changed shape and "
            "every signature claim in this repository needs re-checking."
        )


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "Administrator can perform the authorized operation"
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuthorisedRevocationSucceeds:
    def test_an_administrator_can_revoke_a_role_and_the_role_is_really_gone(self, client, seeded, reader):
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

        response = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver")
        assert response.status_code == 200, response.text
        assert response.json() == {"revoked": 1}

        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Orderer"]

    def test_the_writer_fills_both_revocation_columns_and_names_the_acting_administrator(
        self, client, seeded, reader, raw
    ):
        """
        The schema's `role_assignments_revocation_complete` CHECK requires
        `(revoked_at IS NULL) = (revoked_by IS NULL)`, so a writer that set
        only one would be refused by the database. This asserts the VALUE of
        the one nothing reads: the actor, not the target.
        """
        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver").status_code == 200
        with raw.cursor() as cur:
            cur.execute(
                "SELECT role, revoked_at IS NOT NULL, revoked_by FROM role_assignments "
                "WHERE org_id = %s AND user_id = %s ORDER BY role",
                (seeded["org_a"], seeded["target_a"]),
            )
            rows = cur.fetchall()
        assert rows == [("Approver", True, seeded["admin_a"]), ("Orderer", False, None)], rows

    def test_a_second_revocation_withdraws_nothing_and_says_so(self, client, seeded, reader):
        """DELETE is idempotent in the way DELETE is supposed to be, and the
        count is what makes the two calls distinguishable to a caller."""
        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver").json() == {"revoked": 1}
        second = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver")
        assert second.status_code == 200, second.text
        assert second.json() == {"revoked": 0}
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Orderer"]

    def test_revoking_a_role_the_user_never_held_is_not_a_refusal(self, client, seeded, reader):
        """
        The target exists; the post-condition asked for is already true. A
        404 here would say "no such user", which is false and is an oracle
        about roles rather than about users.
        """
        response = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Auditor")
        assert response.status_code == 200, response.text
        assert response.json() == {"revoked": 0}
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_the_grant_route_and_the_revoke_route_round_trip(self, client, seeded, reader):
        """The pair, end to end, through HTTP only."""
        granted = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "Auditor"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert granted.status_code == 201, granted.text
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Approver", "Auditor", "Orderer"]

        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Auditor").json() == {"revoked": 1}
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "role changes affect authorization correctly" — and WRONG VERSION A
# ═══════════════════════════════════════════════════════════════════════════


class TestRolesAreResolvedPerRequest:
    """
    THE PREMISE OF THE WHOLE DESIGN, PROVED BEHAVIOURALLY.

    If roles were captured into the session at login, every row here would
    fail and the writer would have needed session surgery. They pass because
    `_require_role` re-reads `role_assignments` on every gated call.
    """

    def test_a_granted_role_takes_effect_on_an_already_live_session(self, client, seeded, reader):
        """
        THE POSITIVE HALF, and it has to come first: a session that was
        created BEFORE the grant can use the new capability immediately. That
        is what "resolved per request" means in the direction that cannot be
        confused with a revocation bug.
        """
        before = _revoke(client, seeded["target_a_sid"], seeded["target_a"], "Orderer")
        assert _pair(before) == FORBIDDEN, before.text

        assert (
            client.post(
                f"/admin/users/{seeded['target_a']}/roles",
                json={"role": "Administrator"},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 201
        )

        after = _revoke(client, seeded["target_a_sid"], seeded["target_a"], "Orderer")
        assert after.status_code == 200, after.text
        assert after.json() == {"revoked": 1}

    def test_a_live_session_loses_the_capability_the_moment_the_role_is_revoked(self, client, seeded, reader):
        """
        WRONG VERSION A, CAUGHT HERE. A revoker that marks the row but leaves
        authorisation unaffected passes every "the row changed" assertion and
        fails this one, because this asks the GATE, through the request path,
        on a session that is still live.

        The same session object, the same cookie, three requests: permitted,
        revoked, refused.
        """
        assert (
            client.post(
                f"/admin/users/{seeded['target_a']}/roles",
                json={"role": "Administrator"},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 201
        )
        permitted = _revoke(client, seeded["target_a_sid"], seeded["target_a"], "Orderer")
        assert permitted.status_code == 200, permitted.text

        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Administrator").json() == {"revoked": 1}

        refused = _revoke(client, seeded["target_a_sid"], seeded["target_a"], "Approver")
        assert _pair(refused) == FORBIDDEN, refused.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver"]

    def test_the_session_itself_is_untouched_which_is_why_no_second_mechanism_was_built(self, client, seeded, reader):
        """
        THE OTHER HALF OF THE DESIGN DECISION. Revocation removes ONE
        capability; it does not log the user out. If this method had copied
        `set_user_disabled`'s session UPDATE, the target would lose every
        capability they are still entitled to, and there would be two
        mechanisms enforcing one fact.

        `session_valid` is the domain method `_get_session` itself calls, so
        this asserts validity the way production establishes it.
        """
        from clinical.data_access import Session

        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver").status_code == 200

        still_valid = reader.session_valid(__import__("uuid").UUID(seeded["target_a_sid"]))
        assert isinstance(still_valid, Session)
        assert str(still_valid.user_id) == str(seeded["target_a"])
        assert str(still_valid.org_id) == str(seeded["org_a"])
        # And the capability they still hold still works: Orderer survived.
        assert _roles_of(reader, seeded["target_a_sid"], seeded["target_a"]) == ["Orderer"]

    def test_a_duplicate_grant_does_not_survive_the_revocation(self, client, seeded, reader):
        """
        WRONG VERSION A IN ITS SHARPEST FORM. `_require_role` tests membership
        in a SET of role NAMES, so a revoker keyed on one `assignment_id`
        would mark a row and leave the user holding the role through the
        second grant — a writer whose effect on authorisation is not
        guaranteed. This grants Auditor TWICE and revokes ONCE.
        """
        for _ in range(2):
            assert (
                client.post(
                    f"/admin/users/{seeded['target_a']}/roles",
                    json={"role": "Auditor"},
                    cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
                ).status_code
                == 201
            )
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == [
            "Approver",
            "Auditor",
            "Auditor",
            "Orderer",
        ]

        response = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Auditor")
        assert response.json() == {"revoked": 2}, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]


class TestNoSecondMechanismWasBuilt:
    """
    THE DESIGN DECISION, ASSERTED AS AN ABSENCE IN THE SYNTAX TREE — with the
    twin Standing Rule 4 requires, because an absence assertion is satisfied
    by a scanner that finds nothing anywhere.
    """

    def test_the_writer_touches_no_session_row(self):
        node = _data_access_methods()["revoke_role"]
        offenders = [
            value[:90]
            for value in _body_strings(node)
            if "SESSIONS" in value.upper() or "TERMINATED_AT" in value.upper()
        ]
        assert offenders == [], offenders

    def test_the_twin_the_scanner_finds_the_session_statement_in_set_user_disabled(self):
        """
        w133 put exactly that statement in `set_user_disabled`'s body, for the
        opposite and correct reason: NOTHING on the authenticated path reads
        `users.disabled`, so enforcement did not reach and the sessions had to
        be closed by hand. The scanner finding it there is what makes its
        silence on `revoke_role` mean something.
        """
        node = _data_access_methods()["set_user_disabled"]
        found = [
            value[:40]
            for value in _body_strings(node)
            if "SESSIONS" in value.upper() and "TERMINATED_AT" in value.upper()
        ]
        assert found, [v[:40] for v in _body_strings(node)]

    def test_the_route_handler_mints_terminates_and_composes_nothing(self):
        tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "revoke_role"
        )
        dao_calls = [
            n.func.attr
            for n in ast.walk(handler)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "dao"
        ]
        assert dao_calls == ["revoke_role"], dao_calls


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "non-Administrator denied" — as a SET, over the inputs a
# mis-ordered implementation would answer differently (hazard 4)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheWrongRoleIsRefused:
    def test_a_non_administrator_gets_one_refusal_whatever_it_names(self, client, seeded):
        """
        HAZARD 4, AND THE REASON THIS IS A SET. Wave 2's first version parsed
        the path id in the handler body and RAISED there, so a caller holding
        no Administrator role saw 404 for a malformed id and 403 for a
        well-formed one — the 403 stopped being a function of the caller's
        roles alone.

        The same hazard exists one step further along on this route, because
        it carries a second path segment: a version that validated the ROLE
        vocabulary before the gate would answer 400 for `System` and for a
        nonsense role while answering 403 for `Approver`. Both shapes are in
        the input list, and both collapse the set if present.
        """
        sid = seeded["orderer_a_sid"]
        observed = {
            _pair(_revoke(client, sid, seeded["target_a"], "Approver")),
            _pair(_revoke(client, sid, seeded["target_a"], "Administrator")),
            _pair(_revoke(client, sid, seeded["target_a"], "System")),
            _pair(_revoke(client, sid, seeded["target_a"], "Sorcerer")),
            _pair(_revoke(client, sid, seeded["admin_a"], "Administrator")),
            _pair(_revoke(client, sid, NOWHERE, "Approver")),
            _pair(_revoke(client, sid, MALFORMED, "Approver")),
            _pair(_revoke(client, sid, seeded["target_b"], "Approver")),
            _pair(_revoke(client, sid, seeded["orderer_a"], "Orderer")),
        }
        assert observed == {FORBIDDEN}, observed

    def test_an_auditor_is_not_an_administrator_here_either(self, client, seeded, reader):
        response = _revoke(client, seeded["auditor_a_sid"], seeded["target_a"], "Approver")
        assert _pair(response) == FORBIDDEN, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_a_refused_call_withdraws_nothing(self, client, seeded, reader):
        """STANDING RULE 4: the refusal rows above are absence assertions
        about the RESPONSE. This is the one about the STATE."""
        assert _pair(_revoke(client, seeded["orderer_a_sid"], seeded["target_a"], "Approver")) == FORBIDDEN
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_the_unauthenticated_refusal_is_one_object_too(self, client, seeded):
        """Six ways of not being authenticated, one answer."""
        path = f"/admin/users/{seeded['target_a']}/roles/Approver"
        observed = {
            _pair(client.delete(path)),
            _pair(client.delete(path, cookies={STAFF_COOKIE: "not-a-uuid"})),
            _pair(client.delete(path, cookies={STAFF_COOKIE: NOWHERE})),
            _pair(client.delete(path, cookies={STAFF_COOKIE: ""})),
            _pair(client.delete(path, headers={"Authorization": "Bearer " + NOWHERE})),
            _pair(client.delete(f"/admin/users/{MALFORMED}/roles/Sorcerer")),
        }
        assert observed == {UNAUTHENTICATED}, observed

    def test_the_three_refusals_this_route_can_produce_are_three_distinct_objects_and_no_more(self, client, seeded):
        """
        The whole refusal vocabulary of the route, as a set. Four distinct
        objects exactly: the three shared ones plus the 400 that carries the
        domain's role-vocabulary message — and that last one is reachable
        ONLY by an Administrator of the caller's own organisation, which is
        why it may say more than "Not permitted."
        """
        anonymous = _pair(client.delete(f"/admin/users/{seeded['target_a']}/roles/Approver"))
        forbidden = _pair(_revoke(client, seeded["orderer_a_sid"], seeded["target_a"], "Approver"))
        not_found = _pair(_revoke(client, seeded["admin_a_sid"], NOWHERE, "Approver"))
        bad_role = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Sorcerer")

        assert anonymous == UNAUTHENTICATED
        assert forbidden == FORBIDDEN
        assert not_found == NOT_FOUND
        assert bad_role.status_code == 400, bad_role.text
        assert len({anonymous, forbidden, not_found, _pair(bad_role)}) == 4


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "cross-organisation denied" — WRONG VERSION B
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCrossOrganisationRevocationIsRefused:
    """
    HAZARD 3. Every row here names `target_b`, a REAL user in organisation B
    holding a REAL, live Approver assignment. Without that planted row a
    revoker that dropped its `org_id` predicate would answer 404 anyway —
    because there would be nothing to find — and the class would be green
    against the leak it exists to prove impossible.
    """

    def test_the_collision_is_real_before_anything_is_attempted(self, seeded, reader):
        """The precondition of the class, asserted rather than assumed."""
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == ["Approver"]

    def test_a_real_live_role_in_the_other_organisation_cannot_be_revoked(self, client, seeded, reader):
        response = _revoke(client, seeded["admin_a_sid"], seeded["target_b"], "Approver")
        assert _pair(response) == NOT_FOUND, response.text
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == ["Approver"]

    def test_the_other_tenants_administrator_is_untouched_too(self, client, seeded, reader):
        response = _revoke(client, seeded["admin_a_sid"], seeded["admin_b"], "Administrator")
        assert _pair(response) == NOT_FOUND, response.text
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["admin_b"]) == ["Administrator"]

    def test_the_positive_twin_the_same_request_inside_the_right_organisation_succeeds(self, client, seeded, reader):
        """
        Without this row the class is satisfied by a revoker that refuses
        everybody. The SAME shape of request, by B's own Administrator,
        against B's own user.
        """
        response = _revoke(client, seeded["admin_b_sid"], seeded["target_b"], "Approver")
        assert response.status_code == 200, response.text
        assert response.json() == {"revoked": 1}
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == []

    def test_the_404_is_one_object_over_malformed_absent_and_other_tenant(self, client, seeded):
        """
        NO INFORMATION ORACLE. A real row in another organisation must be
        indistinguishable from an id that names nothing and from an id that
        is not an id — in BOTH channels, as a set.
        """
        sid = seeded["admin_a_sid"]
        observed = {
            _pair(_revoke(client, sid, MALFORMED, "Approver")),
            _pair(_revoke(client, sid, NOWHERE, "Approver")),
            _pair(_revoke(client, sid, seeded["target_b"], "Approver")),
            _pair(_revoke(client, sid, seeded["admin_b"], "Administrator")),
            _pair(_revoke(client, sid, seeded["auditor_b"], "Auditor")),
        }
        assert observed == {NOT_FOUND}, observed

    def test_the_route_takes_no_organisation_anywhere(self, client, seeded, reader):
        """
        DEFENCE BY ABSENT PARAMETER. There is no body on this route at all, so
        the only place a caller could try is the query string — where it is
        ignored, and the write lands in the SESSION's organisation.
        """
        tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
        handler = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "revoke_role"
        )
        named = [a.arg for a in handler.args.args + handler.args.kwonlyargs]
        assert named == ["user_id", "role", "session", "dao"], named

        response = client.delete(
            f"/admin/users/{seeded['target_a']}/roles/Approver?org_id={seeded['org_b']}",
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 200, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Orderer"]
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == ["Approver"]


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "disabled principal cannot continue acting" — WRONG VERSION C
# ═══════════════════════════════════════════════════════════════════════════


class TestTheDisabledAdministratorCannotContinue:
    def test_the_positive_twin_first_the_administrator_can_revoke_while_enabled(self, client, seeded, reader):
        """Without this, the row below is satisfied by a route that refuses
        `admin2_a` for some reason that has nothing to do with disabling."""
        response = _revoke(client, seeded["admin2_a_sid"], seeded["target_a"], "Approver")
        assert response.status_code == 200, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Orderer"]

    def test_the_disabled_administrators_live_session_cannot_revoke_anything(self, client, seeded, reader):
        """
        WRONG VERSION C. The disable and the attempt use the SAME live
        session cookie, so nothing here is re-logging-in: it is the residue
        `set_user_disabled` was written to remove.

        401, not 403 — the session is ENDED by the disable, so the caller is
        anonymous rather than unprivileged, and the body must be the anonymous
        one. Asserting only "it failed" would not tell those apart.
        """
        assert (
            client.post(
                f"/admin/users/{seeded['admin2_a']}/disabled",
                json={"disabled": True},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 204
        )
        response = _revoke(client, seeded["admin2_a_sid"], seeded["target_a"], "Approver")
        assert _pair(response) == UNAUTHENTICATED, response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_disabling_is_not_revoking_and_the_two_mechanisms_stay_distinct(self, client, seeded, reader):
        """
        A disabled user KEEPS their role rows — `set_user_disabled` does not
        touch `role_assignments` and says so. If a later implementer folded
        revocation into disabling, this row fails, and so does the separation
        that makes re-enabling a restoration rather than a re-provisioning.
        """
        assert (
            client.post(
                f"/admin/users/{seeded['admin2_a']}/disabled",
                json={"disabled": True},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 204
        )
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["admin2_a"]) == ["Administrator"]

    def test_a_revoked_administrator_is_refused_while_a_disabled_one_is_anonymous(self, client, seeded):
        """
        THE TWO MECHANISMS, SIDE BY SIDE, AND THEY ARE DIFFERENT OBJECTS.
        Revocation removes a capability: 403. Disabling removes the session:
        401. A version that built session termination into revocation would
        collapse these two into one answer, and this row is what notices.
        """
        assert _revoke(client, seeded["admin_a_sid"], seeded["admin2_a"], "Administrator").json() == {"revoked": 1}
        revoked = _revoke(client, seeded["admin2_a_sid"], seeded["target_a"], "Approver")
        assert _pair(revoked) == FORBIDDEN, revoked.text

        assert (
            client.post(
                f"/admin/users/{seeded['target_a']}/disabled",
                json={"disabled": True},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 204
        )
        disabled = _revoke(client, seeded["target_a_sid"], seeded["target_a"], "Approver")
        assert _pair(disabled) == UNAUTHENTICATED, disabled.text
        assert _pair(revoked) != _pair(disabled)


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "audit event records the correct actor/org/target" — WRONG VERSION D
# ═══════════════════════════════════════════════════════════════════════════


def _audit(client, sid, actions=None):
    params = {"limit": 1000}
    if actions:
        params["action"] = actions
    response = client.get("/admin/audit", params=params, cookies={STAFF_COOKIE: sid})
    assert response.status_code == 200, response.text
    return response.json()


class TestTheAuditRowNamesTheActorTheOrganisationAndTheTarget:
    def test_the_successful_revocation_is_attributed_to_the_acting_administrator(self, client, seeded):
        """
        READ BACK THROUGH THE AUDIT ROUTE, by an Auditor, so the assertion
        goes through the production reader rather than round a table.
        """
        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver").status_code == 200

        rows = [r for r in _audit(client, seeded["auditor_a_sid"], ["role_revoked"])]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["user_id"] == str(seeded["admin_a"]), row
        assert row["org_id"] == str(seeded["org_a"]), row
        assert row["details"]["target_user_id"] == str(seeded["target_a"]), row
        assert row["details"]["role"] == "Approver", row
        assert row["details"]["assignments_revoked"] == 1, row

    def test_the_resource_the_row_is_about_is_the_target_not_the_actor(self, client, seeded, raw):
        """
        `resource_id` has no column in `AuditEntry`, so it is read directly —
        and it is the one value WRONG VERSION D's mirror image gets wrong:
        `@auditable` defaults `resource_id` to the RETURN VALUE, which here is
        a COUNT. Without `resource_id_param`, every revocation in the trail
        would be recorded as resource `1`.
        """
        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Approver").status_code == 200
        with raw.cursor() as cur:
            cur.execute(
                "SELECT resource_id, resource_type, user_id, org_id FROM audit_log WHERE action = 'role_revoked'"
            )
            rows = cur.fetchall()
        assert rows == [(str(seeded["target_a"]), "role_assignment", seeded["admin_a"], seeded["org_a"])], rows

    def test_the_refusal_is_recorded_at_all(self, client, seeded):
        """
        STANDING RULE 4, and the twin for the row below. An audit claim about
        WHERE a refused attempt is filed is vacuous against a version that
        files nothing.
        """
        assert _pair(_revoke(client, seeded["admin_a_sid"], seeded["target_b"], "Approver")) == NOT_FOUND
        rows = _audit(client, seeded["auditor_a_sid"], ["role_revoked"])
        assert len(rows) == 1, rows

    def test_a_refused_cross_organisation_attempt_is_filed_under_the_callers_own_org(self, client, seeded):
        """
        WRONG VERSION D, IN THE ONLY FORM IT CAN TAKE HERE.

        On a SUCCESSFUL revocation the actor's organisation and the target's
        are necessarily the same: `org_id` is bound from the session,
        `get_user` refuses a target outside it, and the composite foreign key
        refuses the row regardless. So the question is only real on a REFUSED
        cross-organisation attempt — and the answer is the ACTOR'S.

        Filing it under the target's would put a row into another tenant's
        audit trail on the strength of a caller with no standing in it, and
        B's Auditor would read an event about their own user caused by a
        principal they cannot see. That is disclosure in the audit channel.
        """
        assert _pair(_revoke(client, seeded["admin_a_sid"], seeded["target_b"], "Approver")) == NOT_FOUND

        a_rows = _audit(client, seeded["auditor_a_sid"], ["role_revoked"])
        b_rows = _audit(client, seeded["auditor_b_sid"], ["role_revoked"])

        assert len(a_rows) == 1, a_rows
        assert a_rows[0]["org_id"] == str(seeded["org_a"]), a_rows
        assert a_rows[0]["user_id"] == str(seeded["admin_a"]), a_rows
        assert b_rows == [], b_rows

    def test_a_refused_non_administrator_attempt_is_attributed_to_the_caller(self, client, seeded):
        assert _pair(_revoke(client, seeded["orderer_a_sid"], seeded["target_a"], "Approver")) == FORBIDDEN
        rows = _audit(client, seeded["auditor_a_sid"], ["role_revoked"])
        assert len(rows) == 1, rows
        assert rows[0]["user_id"] == str(seeded["orderer_a"]), rows
        assert rows[0]["org_id"] == str(seeded["org_a"]), rows

    def test_the_grant_and_the_revocation_are_different_events(self, client, seeded):
        """A trail in which the two are the same action cannot answer "who
        still holds what, and since when"."""
        assert (
            client.post(
                f"/admin/users/{seeded['target_a']}/roles",
                json={"role": "Auditor"},
                cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
            ).status_code
            == 201
        )
        assert _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "Auditor").status_code == 200

        actions = [r["action"] for r in _audit(client, seeded["auditor_a_sid"], ["role_assigned", "role_revoked"])]
        assert actions == ["role_assigned", "role_revoked"], actions


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED: "self-disable/self-destructive operations preserve existing
# rules" — WRONG VERSION E, and the w141 System rule
# ═══════════════════════════════════════════════════════════════════════════


class TestTheExistingRulesArePreserved:
    """
    THE RULES THAT EXIST, FOUND BEFORE THEY WERE TESTED:

    1. `System` IS NOT HUMAN-ASSIGNABLE (w141). `assign_role` refuses it on
       the derived tuple `HUMAN_ASSIGNABLE_ROLES`, with a `ValueError` rather
       than an `AuthorizationError` deliberately — an AuthorizationError would
       say "you may not do this", which invites somebody to look for a
       principal who may, and there is none.

    2. THERE IS NO SELF-TARGETING RULE ANYWHERE. `set_user_disabled` lets an
       Administrator disable their own account; nothing guards a "last
       Administrator"; `_approve_report`'s actor check is about a RECORDED
       AUTHORITY CLAIM, not about self-targeting. The preserved behaviour is
       therefore that self-revocation SUCCEEDS.

    NEITHER IS RE-DERIVED AND NEITHER IS WIDENED. Rule 1 is extended to the
    revoking end of the SAME lifecycle, on the SAME constant — asserted below
    as an identity against `HUMAN_ASSIGNABLE_ROLES`, so a future role added to
    that tuple is covered without anybody editing this file.
    """

    def test_system_is_not_revocable_and_the_refusal_names_the_role(self, client, seeded):
        """
        HAZARD 2: assert WHY. A bare "it was a 4xx" would be green against a
        version that refused because the user did not exist.
        """
        response = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], "System")
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "System" in detail and "internal system principal" in detail, detail
        for role in HUMAN_ASSIGNABLE_ROLES:
            assert role in detail, (role, detail)

    def test_the_revocable_set_is_exactly_the_assignable_set(self, client, seeded):
        """
        ONE CONSTANT, BOTH ENDS OF THE LIFECYCLE. Parameterised over the whole
        of `ROLES`, so this is a census rather than a list somebody kept in
        step by hand: every human-assignable role is accepted, `System` is
        not, and nothing else exists.
        """
        accepted, refused = set(), set()
        for role in ROLES:
            response = _revoke(client, seeded["admin_a_sid"], seeded["target_a"], role)
            (accepted if response.status_code == 200 else refused).add(role)
        assert accepted == set(HUMAN_ASSIGNABLE_ROLES), accepted
        assert refused == set(ROLES) - set(HUMAN_ASSIGNABLE_ROLES) == {"System"}, refused

    def test_assign_role_still_refuses_system_and_this_wave_did_not_touch_it(self, client, seeded, reader):
        """w141, unchanged. The grant end of the rule is asserted here so that
        a change to the shared constant cannot be seen at one end only."""
        response = client.post(
            f"/admin/users/{seeded['target_a']}/roles",
            json={"role": "System"},
            cookies={STAFF_COOKIE: seeded["admin_a_sid"]},
        )
        assert response.status_code == 400, response.text
        assert "cannot be granted to a person" in response.json()["detail"], response.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_an_administrator_may_revoke_their_own_role_and_is_refused_immediately_after(self, client, seeded, reader):
        """
        WRONG VERSION E. The existing rules do NOT speak to self-targeting, so
        the preserved behaviour is that this SUCCEEDS — and, because roles are
        resolved per request, the caller is refused on their very next
        administrative request using the SAME live session.

        A version that invented a "you may not revoke your own last
        Administrator role" guard fails the first assertion. A version that
        marked the row without affecting authorisation fails the second.
        """
        response = _revoke(client, seeded["admin2_a_sid"], seeded["admin2_a"], "Administrator")
        assert response.status_code == 200, response.text
        assert response.json() == {"revoked": 1}

        after = _revoke(client, seeded["admin2_a_sid"], seeded["target_a"], "Approver")
        assert _pair(after) == FORBIDDEN, after.text
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["admin2_a"]) == []

    def test_the_self_revocation_does_not_end_the_callers_session(self, client, seeded, reader):
        """
        The session stays valid — the caller is refused by the GATE, not
        logged out. This is the same distinction as
        `test_a_revoked_administrator_is_refused_while_a_disabled_one_is_anonymous`,
        asserted on the self-targeting path where a "helpful" implementer is
        most likely to add a logout.
        """
        import uuid as _uuid

        assert _revoke(client, seeded["admin2_a_sid"], seeded["admin2_a"], "Administrator").status_code == 200
        session = reader.session_valid(_uuid.UUID(seeded["admin2_a_sid"]))
        assert str(session.user_id) == str(seeded["admin2_a"])

    def test_structurally_the_writer_compares_no_identifier_to_the_callers_own(self):
        """
        THE ABSENCE, PINNED. A self-targeting guard added silently later would
        be a new product rule nobody ruled on, and no behavioural row can see
        a rule that has not been written yet.
        """
        node = _data_access_methods()["revoke_role"]
        offenders = [
            ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Compare) and "session.user_id" in ast.unparse(n)
        ]
        assert offenders == [], offenders

    def test_the_twin_the_comparison_scanner_sees_one_when_it_is_there(self):
        """Standing Rule 4: the row above is satisfied by a scanner that finds
        nothing anywhere."""
        probe = ast.parse("def f(session, target_user_id):\n    return target_user_id == session.user_id\n")
        found = [
            ast.unparse(n)
            for n in ast.walk(probe)
            if isinstance(n, ast.Compare) and "session.user_id" in ast.unparse(n)
        ]
        assert found == ["target_user_id == session.user_id"], found


# ═══════════════════════════════════════════════════════════════════════════
# THE DOMAIN METHOD IS NOT ONLY REACHED THROUGH HTTP
# ═══════════════════════════════════════════════════════════════════════════


class TestTheDomainMethodEnforcesOnItsOwn:
    """
    STANDING RULE 2a, TAKEN SERIOUSLY. Every class above goes through the
    request path, which is right — but it would all still pass if the gate
    lived in the router. These rows call the domain method DIRECTLY, so a
    future caller that is not this router inherits the same enforcement.
    """

    def test_the_gate_is_in_the_method_not_in_the_router(self, clinical_dsn, seeded, reader):
        from clinical.data_access import AuthorizationError

        orderer = reader.session_valid(__import__("uuid").UUID(seeded["orderer_a_sid"]))
        with pytest.raises(AuthorizationError) as exc:
            reader.revoke_role(orderer, seeded["target_a"], "Approver")
        assert "Administrator" in str(exc.value), str(exc.value)
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Approver", "Orderer"]

    def test_the_organisation_predicate_is_in_the_method_too(self, clinical_dsn, seeded, reader):
        from clinical.data_access import NotFoundError

        admin_a = reader.session_valid(__import__("uuid").UUID(seeded["admin_a_sid"]))
        with pytest.raises(NotFoundError):
            reader.revoke_role(admin_a, seeded["target_b"], "Approver")
        assert _roles_of(reader, seeded["admin_b_sid"], seeded["target_b"]) == ["Approver"]

    def test_the_positive_twin_directly_against_the_method(self, clinical_dsn, seeded, reader):
        import uuid as _uuid

        admin_a = reader.session_valid(_uuid.UUID(seeded["admin_a_sid"]))
        assert reader.revoke_role(admin_a, seeded["target_a"], "Approver") == 1
        assert _roles_of(reader, seeded["admin_a_sid"], seeded["target_a"]) == ["Orderer"]
