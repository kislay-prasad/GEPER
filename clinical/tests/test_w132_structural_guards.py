"""
clinical/tests/test_w132_structural_guards.py
──────────────────────────────────────────────
w132 — the guards that keep the shape after the wave is forgotten. Tests
only; no production code depends on this file.

Each row here catches a change that is ONE LINE, looks like an improvement,
and that no behavioural test in this repository would fail on:

  * a second route setting its own cookie, with its own flags;
  * a CORS middleware added "so the portal can call the API";
  * a route mounted that nobody counted (Standing Rule 9 — and see
    `TestTheRouteCensus` for why `app.routes` is not the naive answer it
    looks like on this version of FastAPI);
  * `ConfigurationError` dropped from the login handler's except clause,
    which reopens the MFA status oracle the moment somebody also tidies away
    the broad backstop underneath it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
TREE = CLINICAL.parent


# ═══════════════════════════════════════════════════════════════════════════
# THE ANCHOR (w132c) — the cannot-fail shape, applied to these guards
# themselves.
# ═══════════════════════════════════════════════════════════════════════════
#
# EVERY GUARD IN THIS FILE READS SOURCE BY PATH, and a path is a claim about
# where something is. When the claim is wrong the guard does not fail — it
# reads an empty directory, finds zero offenders, and reports green. That is
# the Phase 5d cannot-fail shape exactly: a test satisfied by the broken state
# as readily as by the working one, except that here the "broken state" is the
# test's own footing.
#
# THIS IS NOT HYPOTHETICAL ON THIS REPOSITORY. Four concrete ways the footing
# moves, none of which any assertion below would otherwise notice:
#
#   * THE TWO CI JOBS RUN FROM DIFFERENT WORKING DIRECTORIES — the clinical
#     job from `clinical/`, the geper job from `geper/`. A path built by
#     parent traversal from `__file__` is stable under that; a path built from
#     the process's cwd is not, and the two are one careless edit apart.
#   * A MOVED TEST FILE. `parent.parent` is a distance, not a destination.
#     Move this file one directory deeper and `CLINICAL` becomes `clinical/`
#     — no, it becomes `clinical/tests/`, whose `rglob("*.py")` finds only
#     tests and whose `auth_endpoints.py` does not exist.
#   * A RENAMED PACKAGE. `TREE.rglob` over a tree with no `clinical/` in it
#     returns files, so a count-based sanity check still passes; what
#     disappears is the specific module every guard is about.
#   * AN INSTALLED SECOND COPY. The guards read a file from disk while the
#     application under test imports `clinical.auth_endpoints` from
#     site-packages. Both succeed, and they are about different code.
#
# So: every path this file uses goes through `_anchor()`, which names LANDMARK
# FILES rather than counting anything, and the anchor has its own negative
# twin (`TestTheAnchorItself`) proving it fails on an empty tree. A guard that
# cannot find its target now FAILS LOUDLY instead of passing quietly.

# Files that must exist for ANY guard in this file to mean anything. Landmarks,
# not a census: the list is short, specific, and spans both packages, so it
# cannot be satisfied by an arbitrary Python tree.
_LANDMARKS = (
    "clinical/app.py",
    "clinical/endpoints.py",
    "clinical/portal_endpoints.py",
    "clinical/auth_endpoints.py",
    # w152. The administrative surface joins the landmark list because every
    # absence-shaped guard in this file (no CORS, no second cookie setter) is
    # vacuous over a tree that does not contain it, and it is now one of the
    # four route modules.
    "clinical/admin_endpoints.py",
    "clinical/data_access.py",
    "clinical/schema.sql",
    "geper/api/main.py",
)


def _anchor(root=None):
    """
    Confirm `root` really is this repository's tree, or raise.

    Returns the root so call sites can write `_anchor() / "clinical"` and
    cannot accidentally skip it.
    """
    root = TREE if root is None else pathlib.Path(root)
    missing = [rel for rel in _LANDMARKS if not (root / rel).is_file()]
    if missing:
        raise AssertionError(
            f"THE GUARDS ARE NOT ANCHORED. Reading from {root}, which is missing {missing}. "
            "These structural tests read source by path; a path that resolves somewhere "
            "without the modules they are about does not make them fail, it makes them find "
            "nothing and pass. Fix the path -- do not relax the landmark list."
        )
    return root


def _source(relative):
    """The text of one file under the anchored tree, proved non-empty."""
    path = _anchor() / relative
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} exists but is empty -- every guard reading it would pass vacuously"
    return text


def _python_files():
    """
    Every Python file in the tree, ANCHORED AND PROVED NON-VACUOUS.

    The census guards below are set comparisons ("exactly these modules call
    `set_cookie`"), and a set comparison against an empty census fails loudly
    for the *equality* rows and silently for the *absence* rows. The
    membership assertion is what covers the absence rows.
    """
    root = _anchor()
    found = [path for path in root.rglob("*.py") if "__pycache__" not in path.parts and ".git" not in path.parts]
    relative = {path.relative_to(root).as_posix() for path in found}
    expected = {rel for rel in _LANDMARKS if rel.endswith(".py")}
    assert expected <= relative, (
        f"the file walk under {root} did not find {sorted(expected - relative)} -- "
        "it is walking the wrong tree, and every absence-shaped guard built on it is vacuous"
    )
    return found


def _calls_named(path, attribute):
    """Actual CALLS of `x.<attribute>(...)`, read from the AST.

    Not a substring search, and this file is the reason the distinction is
    written down: `auth_endpoints.py`'s own docstring contains the words
    `set_cookie` and `_create_system_session`, so a grep-shaped guard reports
    the prose as a call and fails green.

    A FILE THAT CANNOT BE PARSED IS AN ERROR, NOT A ZERO (w132c). This used to
    swallow `SyntaxError` and `UnicodeDecodeError` and return 0, which is the
    anchoring defect in miniature: a mojibaked or half-written module — Rule 7
    names exactly how one gets that way on this toolchain — would report "does
    not call set_cookie" and every census here would agree with it.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise AssertionError(
            f"{path} could not be parsed ({exc!r}), so this guard cannot say anything about it. "
            "Reporting zero calls here would be a silent pass."
        ) from exc
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute
    )


def _calls_of(source, function_name):
    """
    Every call of a bare `function_name(...)`, as (positional args, keywords),
    with each argument rendered back to source.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function_name:
            found.append(
                (
                    [ast.unparse(a) for a in node.args],
                    {kw.arg: ast.unparse(kw.value) for kw in node.keywords},
                )
            )
    return found


class TestTheAnchorItself:
    """
    STANDING RULE 4 APPLIED TO THE FOOTING. Without these rows the anchor is
    an assertion nobody has watched fail, which is the same thing as no
    anchor: `_anchor()` could be `return root` and every guard in this file
    would still be green.
    """

    def test_the_anchor_accepts_the_real_tree(self):
        assert _anchor() == TREE

    def test_the_anchor_refuses_an_empty_directory(self, tmp_path):
        with pytest.raises(AssertionError) as caught:
            _anchor(tmp_path)
        assert "NOT ANCHORED" in str(caught.value)

    def test_the_anchor_refuses_a_tree_that_is_one_directory_off(self, tmp_path):
        """
        THE REALISTIC FAILURE, and the one a mere `path.exists()` check misses:
        a root that is a plausible Python tree with the right NAMES in the
        wrong PLACES. Here `clinical/` exists and `geper/api/main.py` does not,
        which is what a guard pointed at `clinical/` itself would see.
        """
        (tmp_path / "clinical").mkdir()
        for name in ("app.py", "endpoints.py", "portal_endpoints.py", "auth_endpoints.py", "data_access.py"):
            (tmp_path / "clinical" / name).write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "clinical" / "schema.sql").write_text("-- x\n", encoding="utf-8")

        with pytest.raises(AssertionError) as caught:
            _anchor(tmp_path)
        assert "geper/api/main.py" in str(caught.value)

    def test_the_file_walk_refuses_a_tree_without_the_modules(self, tmp_path, monkeypatch):
        """
        `_python_files()` is the base of every census row. Pointed at a tree
        with no `clinical/` it must fail rather than return an empty list --
        the empty list is what makes `assert not offenders` pass forever.
        """
        monkeypatch.setattr("clinical.tests.test_w132_structural_guards.TREE", tmp_path)
        with pytest.raises(AssertionError):
            _python_files()

    def test_the_call_matcher_refuses_a_file_it_cannot_parse(self, tmp_path):
        broken = tmp_path / "broken.py"
        broken.write_text("def f(:\n", encoding="utf-8")
        with pytest.raises(AssertionError) as caught:
            _calls_named(broken, "set_cookie")
        assert "could not be parsed" in str(caught.value)

    def test_the_source_reader_refuses_an_empty_file(self, tmp_path, monkeypatch):
        (tmp_path / "empty.py").write_text("", encoding="utf-8")
        monkeypatch.setattr("clinical.tests.test_w132_structural_guards._anchor", lambda root=None: tmp_path)
        with pytest.raises(AssertionError) as caught:
            _source("empty.py")
        assert "empty" in str(caught.value)


class TestTheGuardsReadTheCodeThatRuns:
    """
    THE OTHER HALF OF ANCHORING, and the one the landmark list cannot cover.
    A landmark check proves a file exists at a path. It does NOT prove that
    the path is where Python loads the module from — and if the application
    under test imports `clinical.auth_endpoints` from an installed copy while
    these guards read the working tree, both succeed and they are about
    different code. Every behavioural test in the wave would then be testing
    one file and every structural test pinning another.
    """

    def test_each_module_these_guards_read_is_the_module_python_imports(self):
        import clinical.admin_endpoints
        import clinical.app
        import clinical.auth_endpoints
        import clinical.data_access
        import clinical.endpoints
        import clinical.portal_endpoints

        for module, relative in (
            (clinical.app, "clinical/app.py"),
            (clinical.endpoints, "clinical/endpoints.py"),
            (clinical.admin_endpoints, "clinical/admin_endpoints.py"),
            (clinical.portal_endpoints, "clinical/portal_endpoints.py"),
            (clinical.auth_endpoints, "clinical/auth_endpoints.py"),
            (clinical.data_access, "clinical/data_access.py"),
        ):
            loaded = pathlib.Path(module.__file__).resolve()
            assert loaded == (TREE / relative), (
                f"{module.__name__} is imported from {loaded}, but these guards read "
                f"{TREE / relative}. They are pinning a file the tests do not run."
            )


class TestCookiePolicyLivesInExactlyOneModule:
    """
    Before w132, `set_cookie` occurred ZERO times in this repository. The
    failure this guard catches is the natural next one: a later route setting
    its own cookie, with its own flags, in its own file — so that "the cookie
    policy" becomes two policies and the weaker one is the one nobody reads.
    """

    def test_only_the_auth_module_sets_a_cookie(self):
        setters = {
            path.relative_to(TREE).as_posix(): _calls_named(path, "set_cookie")
            for path in _python_files()
            if _calls_named(path, "set_cookie")
        }
        assert set(setters) == {"clinical/auth_endpoints.py"}, setters

    def test_the_twin_the_matcher_finds_the_calls_that_exist(self):
        """
        Standing Rule 4: without this, the row above is satisfied by a matcher
        that finds nothing anywhere — which is exactly what a grep for the
        wrong symbol does.
        """
        assert _calls_named(_anchor() / "clinical/auth_endpoints.py", "set_cookie") >= 1

    def test_the_cookie_helper_is_the_only_caller_inside_that_module(self):
        """One module is not enough if the module itself has two policies:
        both routes must go through `_set_session_cookie`."""
        tree = ast.parse(_source("clinical/auth_endpoints.py"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            direct = [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "set_cookie"
            ]
            if direct:
                assert node.name == "_set_session_cookie", (
                    f"{node.name} sets a cookie directly instead of going through "
                    "_set_session_cookie -- that is how two cookie policies begin"
                )


class TestThePerimeterIsStillEmpty:
    """
    The honest negative form of w131's X1. The clinical app adds NO
    middleware, and the reason is recorded in `app.py`: a fail-open layer
    above a fail-closed one only creates a configuration in which the outer
    check is off and looks on.

    The specific thing not to copy is `geper/api/main.py`'s CORS, which
    defaults to `allow_origins=["*"]` with `allow_credentials=True`. This
    guard means adding CORS to a service holding patient data is a deliberate,
    reviewed act rather than a silent widening.
    """

    def test_the_clinical_app_adds_no_middleware(self):
        from clinical.app import app

        assert app.user_middleware == [], app.user_middleware

    def test_the_twin_the_check_detects_one_that_is_added(self):
        """
        Standing Rule 4. `user_middleware` could be the wrong attribute on a
        future Starlette and would then be empty forever. A middleware is
        added to a THROWAWAY app and the same check must see it.
        """
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware

        probe = FastAPI()
        assert probe.user_middleware == []
        probe.add_middleware(CORSMiddleware, allow_origins=["*"])
        assert probe.user_middleware != [], (
            "the check cannot see a middleware that was added, so the row above proves nothing"
        )

    def test_no_cors_import_reaches_the_clinical_package(self):
        """
        ANCHORED (w132c). This is an ABSENCE row — "no file contains
        CORSMiddleware" — and an absence row over an empty walk is the purest
        vacuous pass there is. The scanned set is therefore both counted and
        named: the landmark modules must be among the files actually opened.
        """
        scanned = []
        for path in (_anchor() / "clinical").rglob("*.py"):
            if "__pycache__" in path.parts or path.parts[-2:-1] == ("tests",):
                continue
            scanned.append(path)
            source = path.read_text(encoding="utf-8")
            assert "CORSMiddleware" not in source, path

        names = {path.name for path in scanned}
        assert {
            "app.py",
            "endpoints.py",
            "portal_endpoints.py",
            "auth_endpoints.py",
            "admin_endpoints.py",
        } <= names, (
            f"the scan opened {sorted(names)} -- it is not reading the clinical package, "
            "so finding no CORS import proves nothing"
        )


def served_routes(app):
    """
    EVERY (path, methods) the app actually serves.

    STANDING RULE 9, AND A CORRECTION TO THE OBVIOUS READING OF IT. "Enumerate
    `app.routes`" is right in intent and NOT sufficient as written on this
    version of FastAPI (0.141.1 / starlette 1.6.0): `include_router` leaves an
    `_IncludedRouter` WRAPPER in `app.routes` rather than flattening the
    router's routes into it. Iterating `app.routes` on this app returns FIVE
    objects — three opaque wrappers, `/healthz` and `/readyz` — so a census
    written the obvious way sees neither `/portal/me` nor `/auth/login` and
    reports a surface of two.

    That is the same defect as counting `@router.` decorators, one layer
    along: a census that cannot see what the framework did.
    """
    found = []
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(served_routes_of(inner))
        else:
            found.append((getattr(route, "path", None), tuple(sorted(getattr(route, "methods", None) or []))))
    return sorted(set(found))


def served_routes_of(router):
    found = []
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(served_routes_of(inner))
        else:
            found.append((getattr(route, "path", None), tuple(sorted(getattr(route, "methods", None) or []))))
    return found


class TestTheRouteCensus:
    """
    The row that would have caught the eight invisible documentation routes,
    and that now also catches the two this wave added had nobody declared
    them. Standing Rule 3 in test form: the definite article is a tell, and a
    census needs counting.
    """

    EXPECTED = sorted(
        {
            ("/healthz", ("GET",)),
            ("/readyz", ("GET",)),
            ("/exceptions/orders/{order_id}", ("GET",)),
            ("/exceptions/worklist", ("GET",)),
            ("/exceptions/{exception_id}", ("PATCH",)),
            ("/portal/me", ("GET",)),
            ("/portal/me/tests", ("GET",)),
            # w135. The two patient report routes. They carry METADATA AND
            # RELEASE STATE and nothing else, because `reports` has no content
            # column — see clinical/portal_endpoints.py's header. The second is
            # the only route on the patient surface that takes an identifier
            # from the caller, and it is a RESOURCE id, never an identity one.
            ("/portal/me/reports", ("GET",)),
            ("/portal/me/reports/{report_id}", ("GET",)),
            # w136. The patient's own released reports AS A SERIES. It takes NO
            # identifier at all — it is deliberately NOT the held
            # `GET /patients/{patient_id}/variant-trends`, which is a STAFF
            # route on a staff router, and which this census would also catch
            # had anybody mounted it.
            ("/portal/me/trends", ("GET",)),
            ("/auth/login", ("POST",)),
            ("/portal/login", ("POST",)),
            # w132c. The two logout routes. They are NOT on the auth router:
            # logout consumes a session, so each lives in the module that owns
            # its principal's dependency — /auth/logout on a second, unprefixed
            # router in endpoints.py, /portal/logout on the portal router.
            ("/auth/logout", ("POST",)),
            ("/portal/logout", ("POST",)),
            # w152 (WAVE 2). THE FIRST ADMINISTRATIVE ROUTES THIS SERVICE HAS
            # EVER SERVED. Before them the surface was seventeen production
            # routes and ZERO administrative ones, which is the single reason
            # thirteen of fourteen unreachable capabilities were unreachable.
            #
            # Each is ONE existing domain method: assign_role,
            # set_user_disabled, set_patient_login_disabled, read_audit. Each
            # authenticates with the STAFF dependency out of endpoints.py and
            # authorises through _require_role inside the domain method.
            #
            # NONE OF THEM IS REACHABLE BY A REAL DEPLOYMENT until Wave 3
            # resolves the first-administrator deadlock; they are tested with
            # a fixture-provisioned Administrator, which is a fixture and not
            # a bootstrap.
            ("/admin/users/{user_id}/roles", ("POST",)),
            # w154 (WAVE 4). THE INVERSE OF THE ROUTE ABOVE, and the first
            # production writer of `role_assignments.revoked_at` /
            # `revoked_by` this repository has ever had: at 4700f40 those two
            # columns appeared at exactly two non-test code sites and BOTH
            # were `revoked_at IS NULL` read predicates. It is one existing
            # shape — one domain method (`revoke_role`), the staff dependency,
            # `_require_role` inside the method — and it ends no sessions,
            # because roles are resolved per request and a second mechanism
            # for an effect the first one already has can only disagree with
            # it. See clinical/tests/test_w154_role_revocation.py.
            ("/admin/users/{user_id}/roles/{role}", ("DELETE",)),
            ("/admin/users/{user_id}/disabled", ("POST",)),
            # w155 (WAVE 5). PATIENT CREDENTIAL ISSUANCE, the pair-mate of the
            # revocation route below, which w152 mounted alone. It is the
            # SECOND of the two safe route shapes: an authenticated
            # Administrator NAMING A TARGET, with the organisation taken from
            # `Session.org_id`. It is deliberately not the portal shape --
            # `/portal/me/trends` takes no identifier at all -- and it must
            # never acquire a sibling that takes a patient identifier without
            # a staff session, which is what
            # `test_w155_patient_administration.py::TestNoPublicPatientSelfRegistration`
            # checks against this same census.
            ("/admin/patients/{patient_id}/logins", ("POST",)),
            ("/admin/patient-logins/{patient_login_id}/disabled", ("POST",)),
            ("/admin/audit", ("GET",)),
            # w156 (WAVE 6). REPORT RETRACTION, and it is the first route on
            # this surface for a capability that was ABSENT rather than
            # implemented-and-unreachable: at 7bb6f8c `def retract_report` had
            # ZERO definitions in production (measured by syntax tree over all
            # 299 non-test modules), while migration 0003 had already landed
            # `report_retractions` and `retraction_notifications`. The verb was
            # written this wave, in this tree's idiom, and NOT lifted from the
            # held branch `w122-retraction` — see the note above the method in
            # clinical/data_access.py for the reasoning and for which rulings
            # (E1, E3-E8) the schema had already settled.
            #
            # APPROVER, NOT ADMINISTRATOR. The second route here whose role is
            # not Administrator (`/admin/audit` is Auditor): the prefix names
            # the surface, not the gate. Every verb in this platform that acts
            # on an approved report is Approver-gated, so this one is too, and
            # no eighth role was invented.
            #
            # THE EXCEPTION-AMENDMENT ROUTE IS DELIBERATELY NOT HERE. That verb
            # cannot be written without widening `exception_events.action`'s
            # CHECK, which is a destructive schema change; w156 STOPPED and
            # carded it rather than inventing a vocabulary.
            ("/admin/reports/{report_id}/retraction", ("POST",)),
            # w157 (WAVE 7). THE LABORATORY BENCH SURFACE -- the first routes
            # this service has ever served for ANY operational stage of the
            # `Order -> Sample -> Processing -> QC -> Interpretation -> Review
            # -> Report` chain. Before them the served surface touched exactly
            # one stage of that chain, and only its last verb (retraction).
            #
            # Measured at 764d646 by an ALIAS-RESOLVING ast.Call census over
            # every non-test module -- a bare-name scan misses `from x import
            # f as _f`, and grep credits prose -- `receive_sample` and
            # `record_qc` each had ZERO production call sites while both were
            # already gated, state-checking, audited and domain-tested.
            #
            # LAB TECHNICIAN, NOT ADMINISTRATOR AND NOT APPROVER. The third
            # distinct role to reach this service's surface, and the prefix
            # `/lab` names the surface rather than the gate, exactly as
            # `/admin` does for `/admin/audit` (Auditor) and the retraction
            # route (Approver).
            #
            # THE ORDER STAGE IS DELIBERATELY NOT HERE. `create_order`,
            # `place_order` and `cancel_order` are all Orderer-gated, complete
            # and callerless, but `create_order` requires a `test_id` foreign
            # key into `tests` -- a table with no `DataAccess` method of any
            # kind, measured over every method of the class by syntax tree. Who
            # may define a test panel is a product decision nobody has made, so
            # w157 STOPPED and carded the Order stage rather than mounting the
            # middle of a severed chain.
            #
            # AND THE RELEASE ROUTE IS DELIBERATELY NOT HERE EITHER, which is
            # the larger absence. `_release_report` still has zero production
            # call sites, so the three patient report surfaces still return
            # empty for every patient. Who may release and from which state are
            # both derivable; the `consumer` is not, and `schema.sql`'s own
            # comment on that column says why -- "free text pending the
            # delivery paths of a later commit; enumerating it now would fix a
            # vocabulary before the paths exist". w157 declined to fix it by
            # accident. See clinical/workflow_endpoints.py.
            ("/lab/orders/{order_id}/samples", ("POST",)),
            ("/lab/samples/{sample_id}/qc", ("POST",)),
            # w162. THE ORDERING CLINICIAN'S SURFACE, and the THIRD prefix
            # this service serves. Patient registration and consent are the
            # two acts that precede an order, and at 44d624b all three verbs
            # behind these routes had ZERO production call sites AND no role
            # gate of any kind -- every authenticated session in the
            # organisation could mint a patient identity.
            #
            # ORDERER, NOT ADMINISTRATOR AND NOT LAB TECHNICIAN. The human
            # ruled it on 2026-09-19 ("the ORDERER/CLINICIAN owns both"), and
            # the token was resolved against `ROLES`, the vocabulary the
            # schema's CHECK on `role_assignments.role` enumerates. The fourth
            # distinct role to reach this service's surface.
            #
            # WHY NOT `/admin` OR `/lab`: the prefix names the surface, never
            # the gate. `/admin` would encode in every access log the reading
            # the ruling rejects -- that registering a patient is an
            # administrative act -- and `/lab` names the bench, whose role the
            # ruling explicitly bars from creating a patient identity.
            #
            # THE ORDER STAGE IS STILL DELIBERATELY NOT HERE, for w157's
            # unchanged reason: `create_order` needs a `test_id` foreign key
            # into `tests`, a table with no `DataAccess` writer of any kind.
            # When that unblocks, `/clinic` is where it belongs.
            ("/clinic/patients", ("POST",)),
            ("/clinic/patients/{patient_id}/consents", ("POST",)),
            ("/clinic/consents/{consent_id}/withdrawal", ("POST",)),
            # Stage 2 (test catalogue and order stage). The card the w157 and
            # w162 notes above describe -- "who may define a test panel" -- was
            # ruled: the Administrator manages a per-organisation catalogue
            # (`create_test`, `retire_test`), the Orderer and Administrator read
            # it, and the Orderer creates, places and cancels orders against it.
            # Six routes; every one is a single domain call behind the staff
            # session dependency and none takes an organisation.
            ("/admin/tests", ("POST",)),
            ("/admin/tests/{test_id}/retirement", ("POST",)),
            ("/clinic/tests", ("GET",)),
            ("/clinic/orders", ("POST",)),
            ("/clinic/orders/{order_id}/placement", ("POST",)),
            ("/clinic/orders/{order_id}/cancellation", ("POST",)),
            # Stage 3. THE REVIEW SURFACE -- the fourth prefix. Five routes,
            # each one domain call behind the staff session dependency: the
            # Interpreter records a claim (concurrence or disagreement) and
            # submits; the Approver approves and releases. The gates are the
            # domain methods'; see clinical/review_endpoints.py.
            ("/review/reports/{report_id}/concurrence", ("POST",)),
            ("/review/reports/{report_id}/disagreement", ("POST",)),
            ("/review/reports/{report_id}/submission", ("POST",)),
            ("/review/reports/{report_id}/approval", ("POST",)),
            ("/review/reports/{report_id}/release", ("POST",)),
            # w164. THE SECOND FACTOR. `enrol_totp` has been
            # Administrator-gated, audited and tenant-bound since Phase 1 and
            # had ZERO production call sites and no route -- AND it could not
            # have worked if called, because every production `DataAccess` was
            # built without a cipher and the method raises `ConfigurationError`
            # without one. Both halves landed in the same commit: a route over
            # a method that can only raise is not a surface, and a working
            # method nothing can reach is not a capability.
            #
            # ADMINISTRATOR, on the `/admin` prefix, and it takes NO REQUEST
            # BODY. The domain method used to accept the SECRET as a parameter;
            # it generates one now, so there is no field through which a weaker
            # secret could arrive.
            #
            # THE PATIENT SECOND FACTOR IS DELIBERATELY NOT HERE.
            # `patient_logins` has no TOTP column of any kind, and enrolling a
            # remote patient is a product ruling nobody has made -- see
            # `test_w155_patient_administration.py::TestPatientMfaIsBlocked`.
            ("/admin/users/{user_id}/totp", ("POST",)),
        }
    )

    def test_the_served_surface_matches_the_declared_list_exactly(self):
        from clinical.app import app

        assert served_routes(app) == self.EXPECTED

    def test_the_walker_is_not_returning_an_empty_or_wrapper_shaped_answer(self):
        """
        Standing Rule 4, and the specific hazard this file documents: a
        walker that failed to descend into `_IncludedRouter` returns five
        entries, three of them `(None, ())`. Both shapes are refused here.
        """
        from clinical.app import app

        census = served_routes(app)
        # 11 at 863f07e; 13 from w135, which added the two patient report
        # routes; 14 from w136, which added the patient report series; 18 from
        # w152, which added the four administrative routes; 19 from w154,
        # which added the role-revocation route; 20 from w155, which added
        # patient credential issuance; 21 from w156, which added report
        # retraction; 23 from w157, which added the two laboratory bench
        # routes; 26 from w162, which added the three clinic routes —
        # registration, consent and withdrawal; 27 from w164, which added TOTP
        # enrolment; 33 from Stage 2, which added the catalogue and order
        # routes; 38 from Stage 3, which added the five review routes. The number is checked against the EXPECTED set above,
        # which is what actually names them — this row exists only to refuse
        # the empty and wrapper-shaped answers.
        assert len(census) == 38, census
        assert all(path and path.startswith("/") for path, _ in census), census
        assert all(methods for _, methods in census), census

    def test_the_documentation_routes_are_not_among_them(self):
        from clinical.app import app

        paths = {path for path, _ in served_routes(app)}
        assert paths.isdisjoint({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}), paths


class TestTheRefusalStaysUniform:
    def test_the_login_handlers_name_ConfigurationError_in_their_except_clause(self):
        """
        RECORDED BECAUSE THE BEHAVIOURAL TEST CANNOT SEE IT, and this was
        measured rather than assumed: removing `ConfigurationError` from
        `_refuse_uniformly`'s except tuple leaves every MFA row in
        `test_w132_staff_login_route.py` GREEN, because the broad `Exception`
        backstop underneath catches it anyway.

        That is fine today and it is one tidy-up away from not being fine: a
        later reviewer who removes the broad catch — a reasonable thing to
        want — would silently restore the 500-vs-401 oracle on MFA-enrolled
        accounts. So the named clause is pinned here in addition to the
        behaviour.
        """
        tree = ast.parse(_source("clinical/auth_endpoints.py"))
        named = set()
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                continue
            types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            named.update(node.id for node in types if isinstance(node, ast.Name))

        assert "ConfigurationError" in named, named
        assert "AuthenticationError" in named, named

    def test_the_anonymous_refusal_is_one_function_and_the_auth_module_uses_it(self):
        """A second hand-written `HTTPException(401, ...)` in the auth module
        is how two refusals that differ by a byte begin.

        THE "USES IT" HALF IS READ FROM THE SYNTAX TREE, NOT FROM A SUBSTRING
        SCAN (w158, Rule 10). It was `assert "_anonymous_refusal" in source`,
        and MEASURED at 9c63fc8 that line matches FOUR places in
        `clinical/auth_endpoints.py`: the import (line 76), a module comment
        (line 363) and the two real raise sites (400, 404). Delete both raise
        sites AND the import and the assertion still passes, on the comment
        alone — so the half of this row's name that says "and the auth module
        uses it" was proved by prose. A substring scan credits prose as a
        caller; `ast.Call` cannot.
        """
        source = _source("clinical/auth_endpoints.py")
        tree = ast.parse(source)

        raised = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_anonymous_refusal"
        ]
        # A FLOOR, NOT A COUNT: a guard that encodes today's number protects
        # today's number. What must never be true is ZERO — an auth module
        # that refuses by some other object is how two refusals diverge.
        assert raised, (
            "no CALL to _anonymous_refusal in clinical/auth_endpoints.py -- the module no longer "
            "refuses through the shared object, and the HTTPException walk below cannot see that"
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HTTPException":
                keywords = {kw.arg: kw for kw in node.keywords}
                status_arg = keywords.get("status_code")
                rendered = ast.unparse(status_arg.value) if status_arg else ""
                assert "401" not in rendered and "UNAUTHORIZED" not in rendered, (
                    "the auth module builds its own 401 instead of using _anonymous_refusal"
                )


class TestTheStaffDependencyWasStillNotWidened:
    """
    The w130 guards, extended to the module this wave added. `_get_session`
    must still be a staff-only dependency, and the new module must not have
    handed a `PatientSession` to it.
    """

    def test_get_session_still_validates_a_staff_session_only(self):
        source = _source("clinical/endpoints.py")
        body = source[source.index("def _get_session(") :]
        body = body[: body.index("\n@router.")]
        assert "dao.session_valid(" in body
        assert "patient_session_valid" not in body
        assert "PatientSession" not in body

    def test_the_auth_module_does_not_import_either_session_dependency(self):
        """
        It has no business validating a session: it MINTS credentials, it does
        not accept them. Importing `_get_session` here would be the first step
        towards a login route that takes a session.
        """
        source = _source("clinical/auth_endpoints.py")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
        assert "_get_session" not in imported, imported
        assert "_get_patient_session" not in imported, imported

    def test_the_twin_the_import_walker_sees_the_imports_that_are_there(self):
        tree = ast.parse(_source("clinical/auth_endpoints.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
        assert {"_get_data_access", "_anonymous_refusal", "_SESSION_COOKIE"} <= imported, imported


# ═══════════════════════════════════════════════════════════════════════════
# w132c — THE LOGOUT SURFACE
# ═══════════════════════════════════════════════════════════════════════════


class TestTheCookieIsClearedInExactlyOnePlaceAndOnTheRightPath:
    """
    The mirror of `TestCookiePolicyLivesInExactlyOneModule`, for the half the
    wave just added. Setting a cookie and clearing one are the same decision
    made twice, and the second one is the easy one to get wrong quietly: a
    browser keys a cookie on (name, domain, path), so a deletion sent on a
    different path adds a second empty cookie and leaves the live credential
    in the jar. Nothing observable breaks — the person appears logged out,
    because the server-side row IS terminated — and the credential the whole
    design calls a bearer token stays in the browser.
    """

    def test_only_one_module_clears_a_cookie(self):
        clearers = {
            path.relative_to(TREE).as_posix(): _calls_named(path, "delete_cookie")
            for path in _python_files()
            if _calls_named(path, "delete_cookie")
        }
        assert set(clearers) == {"clinical/endpoints.py"}, clearers

    def test_the_twin_the_matcher_finds_the_call_that_exists(self):
        """Standing Rule 4: without this the row above is satisfied by a
        matcher that finds nothing anywhere."""
        assert _calls_named(_anchor() / "clinical/endpoints.py", "delete_cookie") >= 1

    def test_the_clear_helper_is_the_only_caller_inside_that_module(self):
        tree = ast.parse(_source("clinical/endpoints.py"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            direct = [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "delete_cookie"
            ]
            if direct:
                assert node.name == "_clear_session_cookie", (
                    f"{node.name} clears a cookie directly instead of going through "
                    "_clear_session_cookie -- that is how two cookie policies begin"
                )

    def test_every_cookie_is_cleared_on_the_path_it_was_set_with(self):
        """
        THE ROW THAT CANNOT BE WRITTEN BEHAVIOURALLY WITHOUT A REAL BROWSER,
        and the reason it is here rather than only in the logout tests: a test
        client's cookie jar does not enforce path scoping, so a mismatched
        clear passes every HTTP assertion in this repository. Read from the
        source instead, as (cookie name, path) pairs, and required to AGREE.
        """
        set_calls = _calls_of(_source("clinical/auth_endpoints.py"), "_set_session_cookie")
        clear_calls = _calls_of(_source("clinical/endpoints.py"), "_clear_session_cookie") + _calls_of(
            _source("clinical/portal_endpoints.py"), "_clear_session_cookie"
        )

        def pairs(calls):
            out = set()
            for args, keywords in calls:
                assert len(args) >= 2, args
                path = keywords.get("path")
                assert path is not None, ("a cookie call with no explicit path", args, keywords)
                out.add((args[1], path))
            return out

        set_pairs = pairs(set_calls)
        clear_pairs = pairs(clear_calls)

        # Non-vacuity FIRST: two principals, two cookies, so two of each. An
        # empty set equals an empty set, which is the failure this whole file
        # is about.
        assert len(set_pairs) == 2, set_pairs
        assert len(clear_pairs) == 2, clear_pairs
        assert set_pairs == clear_pairs, (
            f"a cookie is set as {sorted(set_pairs)} and cleared as {sorted(clear_pairs)}. "
            "A deletion on a different path leaves the live credential in the browser."
        )

    def test_the_two_cookies_are_still_two_and_are_the_w130_names(self):
        """One cookie serving both principals would undo w130's separation,
        and it would arrive looking like a tidy-up of the pair above."""
        named = {args[1] for args, _ in _calls_of(_source("clinical/auth_endpoints.py"), "_set_session_cookie")}
        assert named == {"_SESSION_COOKIE", "_PATIENT_SESSION_COOKIE"}, named


class TestTheLogoutRoutesCannotNameASession:
    """
    THE STRUCTURAL FORM OF THE WRONG VERSION THIS WAVE WAS BUILT AGAINST. The
    behavioural row (`test_the_route_accepts_no_body_that_could_name_a_session`)
    proves today's handler ignores a supplied id. This proves the handler has
    no way to RECEIVE one — the property that survives a refactor, because a
    body model added "so the client can say which session" fails no
    behavioural test until somebody also wires it up, and then it passes all
    of them.
    """

    HANDLERS = (
        ("clinical/endpoints.py", "staff_logout", "session"),
        ("clinical/portal_endpoints.py", "patient_logout", "patient_session"),
    )

    def _handler(self, relative, name):
        for node in ast.walk(ast.parse(_source(relative))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"no handler {name!r} in {relative} -- the guard is reading the wrong file")

    def test_neither_handler_takes_anything_but_its_dependencies(self):
        for relative, name, principal in self.HANDLERS:
            node = self._handler(relative, name)
            params = [a.arg for a in node.args.args]
            assert params == [principal, "dao"], (
                f"{name} takes {params}. A logout that accepts a parameter is a logout that can "
                "be asked to end somebody else's session."
            )
            # Every parameter has a `Depends(...)` default, so none of them is
            # a body or a path parameter FastAPI would fill from the request.
            defaults = [ast.unparse(d) for d in node.args.defaults]
            assert len(defaults) == len(params), (params, defaults)
            assert all(d.startswith("Depends(") for d in defaults), defaults

    def test_neither_route_declares_a_path_parameter(self):
        """
        The route PATH only — not the whole decorator, whose `responses=`
        dictionary contains braces of its own and would make this row pass or
        fail for the wrong reason.
        """
        for relative, name, _ in self.HANDLERS:
            node = self._handler(relative, name)
            paths = [
                d.args[0].value
                for d in node.decorator_list
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
            ]
            assert len(paths) == 1, (name, paths)
            assert "{" not in paths[0], (name, paths)
            assert paths[0].endswith("logout"), (name, paths)

    def test_each_handler_calls_its_own_principals_termination_and_no_other(self):
        """
        `terminate_session` is the ADMINISTRATOR-GATED method that ends
        SOMEBODY ELSE'S session. A logout handler reaching it would be that
        capability behind a route anybody can call, and the two names differ
        by one word.
        """
        expected = {
            "staff_logout": "terminate_my_session",
            "patient_logout": "terminate_my_patient_session",
        }
        for relative, name, _ in self.HANDLERS:
            node = self._handler(relative, name)
            called = {
                call.func.attr
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            }
            assert expected[name] in called, (name, called)
            assert "terminate_session" not in called, (
                f"{name} calls terminate_session -- the Administrator-gated method that ends ANOTHER session"
            )
            other = expected["patient_logout" if name == "staff_logout" else "staff_logout"]
            assert other not in called, (name, called)


class TestTheTerminationMethodsTakeNoTarget:
    """
    The same property one layer down, where it actually holds. A route guard
    can be satisfied by a handler that reads an id from somewhere else; the
    METHOD having no parameter to put one in cannot.

    Read from the AST of `data_access.py` rather than `inspect.signature`,
    because `@auditable` wraps every public method in a bare `def
    wrapper(self, *args, **kwargs)` with no `functools.wraps` — so
    `inspect.signature` reports `['self', 'args', 'kwargs']` for every method
    in that layer, identically, whatever it declares.
    """

    EXPECTED = {
        "terminate_my_session": ["self", "session"],
        "terminate_my_patient_session": ["self", "patient_session"],
        # The contrast, pinned deliberately: the administrative method DOES
        # take a target, and must keep doing so. Without this row the class
        # above is satisfied by a matcher that finds no parameters anywhere.
        "terminate_session": ["self", "session", "target_session_id"],
    }

    def test_each_termination_method_declares_exactly_its_expected_parameters(self):
        tree = ast.parse(_source("clinical/data_access.py"))
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]
        assert classes, "no class DataAccess -- the guard is reading the wrong file"

        found = {
            node.name: [a.arg for a in node.args.args]
            for node in classes[0].body
            if isinstance(node, ast.FunctionDef) and node.name in self.EXPECTED
        }
        assert found == self.EXPECTED, found

    def test_the_self_logout_methods_require_no_role(self):
        """
        `_require_role` in a self-logout would mean only Administrators could
        log out. The administrative method must still carry it, which is the
        twin that stops this row passing against a matcher that finds nothing.
        """
        tree = ast.parse(_source("clinical/data_access.py"))
        bodies = {
            node.name: ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in self.EXPECTED
        }
        assert "_require_role" not in bodies["terminate_my_session"], bodies["terminate_my_session"]
        assert "_require_role" not in bodies["terminate_my_patient_session"]
        assert "_require_role(session, 'Administrator')" in bodies["terminate_session"], (
            "terminate_session lost its Administrator gate -- this wave must not have weakened it"
        )


class TestTheLoginModuleStillMintsAndDoesNotConsume:
    """
    w132c did NOT put the logout routes in `auth_endpoints.py`, and this pins
    the reason rather than leaving it in a commit message. That module mints
    credentials; the guard forbidding it to import a session dependency is
    what stops a login route growing into one that accepts a session, and the
    obvious place to have put logout would have forced that guard to be
    relaxed.
    """

    def _decorators(self):
        source = _source("clinical/auth_endpoints.py")
        found = [
            ast.unparse(d)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for d in node.decorator_list
        ]
        assert found, "no decorated functions found -- the guard is reading the wrong file"
        return found

    def test_the_auth_module_declares_no_logout_route(self):
        decorators = self._decorators()
        assert not any("logout" in d for d in decorators), decorators

    def test_the_twin_the_scan_sees_the_login_routes_that_are_there(self):
        decorators = self._decorators()
        assert sum("/auth/login" in d for d in decorators) == 1, decorators
        assert sum("/portal/login" in d for d in decorators) == 1, decorators
