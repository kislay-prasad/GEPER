"""
clinical/tests/test_w153_first_administrator_bootstrap.py
─────────────────────────────────────────────────────────
w153 (WAVE 3) — THE FIRST-ADMINISTRATOR BOOTSTRAP, and what stops it being a
permanent bypass.

WHAT AUTHORISES THIS, SAID FIRST, BECAUSE THE WAVE IS ALLOWED TO BE A STOP.
`clinical/bootstrap.py` is NOT the mechanism and this file does not use it:
read rather than inferred from its name, it creates the two POSTGRES LOGIN
ROLES (`clinical_app`, `clinical_retention`) that `schema.sql` grants to, and
optionally applies `schema.sql`. It writes no `organisations` row, no `users`
row and no `role_assignments` row, and it knows nothing about application
principals. It is a DATABASE bootstrap, not an IDENTITY one.

The authorised mechanism is the human's own w128 ruling
(`hive/agents/god/briefs/w128-provisioning.md`), which specifies exactly this
operation — "organisation row → first user → first Administrator role
assignment, **in one transaction**", session-free "because it is called before
any session can exist", operator-run, and with **no HTTP route**. This file is
that ruling's required tests plus the three Wave 3 adds: a positive twin that
goes all the way through a WAVE 2 ROUTE, the inertness proof, and the
structural guard that keeps the method off every served surface.

─── WHAT MAKES IT INERT ONCE AN ADMINISTRATOR EXISTS ───

Not a flag. Three structural facts, each proved from the database or the
syntax tree rather than from anything a caller supplies:

  1. IT CANNOT NAME AN ORGANISATION THAT ALREADY EXISTS. `org_id` is
     `uuid.uuid4()` in the method body and there is NO parameter of any kind
     by which a caller could name an existing one. So for every organisation
     that already has its Administrator, this method is a no-op it cannot
     address — the house "defence by absent parameter" pattern, the same one
     `terminate_my_session` and `list_my_report_trend` use, where the wrong
     version is UNWRITABLE rather than merely refused.
  2. IT CANNOT GRANT ANY ROLE BUT `'Administrator'`. The role is a SQL
     LITERAL, not a parameter, so `'System'` — which w141 made unassignable
     to people — cannot be reached through it either.
  3. IT IS ON NO SERVED SURFACE. No route in either app reaches it; the only
     caller is an operator who already holds the database credential and who
     could therefore already write these three rows by hand. That is the
     whole reason this is not a privilege escalation: it grants nobody any
     capability they did not already have, and converts an out-of-band raw
     INSERT (which twenty-six test files carry) into one audited atomic act.

A SECOND CALL IS NOT "REFUSED", AND SAYING SO WOULD BE FALSE. It creates a
SECOND, DIFFERENT organisation with that organisation's own first
Administrator — which is the spec's single-instance multi-organisation
tenancy (§4.2), not a bypass. What is refused is the only thing that would be
a bypass: reaching an EXISTING organisation. `TestItCannotBeRepeatedAgainstAn
OrganisationThatAlreadyHasAnAdministrator` proves that with the same name and
the same administrator email, and reads the first organisation back THROUGH
THE DOMAIN METHODS to show it is untouched.

─── THE MEASURED HAZARD THIS FILE IS SHAPED AROUND ───

`@auditable` CALLS `commit()` ITSELF after writing each audit row, OUTSIDE
`@transactional`'s `_in_transaction` re-entrancy guard. So a
`provision_organisation` COMPOSED out of the already-decorated
`create_organisation` and `create_user` would have each inner wrapper commit
mid-way and atomicity would be silently gone: a failure at the role step would
leave a committed organisation and a committed user with NO ADMINISTRATOR —
the exact unusable state the method exists to prevent.

THE TRANSACTION BOUNDARY, stated: `@transactional` on
`provision_organisation` itself. `@auditable` is the OUTER decorator, so on an
unexpected exception `@transactional` has already ROLLED BACK before
`@auditable`'s error branch writes and commits its row. The data goes; the
evidence stays. `TestAFailureAtTheLastStepLeavesNothing` forces the failure at
step three and reads back through the domain methods.

─── THE WRONG-FIRST LIST, AND WHICH ROW CATCHES EACH ───

(A) A BOOTSTRAP THAT CAN RUN AGAIN ONCE AN ADMINISTRATOR EXISTS — i.e. one
    that accepts an `org_id` and adds a second Administrator to a live tenant.
    Caught by `TestItCannotBeRepeatedAgainstAnOrganisationThatAlreadyHasAn
    Administrator` behaviourally and by
    `test_it_accepts_no_organisation_identifier_of_any_kind` structurally.
    VACUOUS AGAINST: a mechanism that never works at all — which is why
    `TestTheBootstrapProducesAWorkingAdministrator` comes first in this file.

(B) ONE WHOSE "ALREADY BOOTSTRAPPED" CHECK READS A CALLER-SUPPLIED VALUE —
    an `if already_bootstrapped:` parameter, or a check against a name or an
    email the caller chose.
    Caught by `test_the_organisation_id_is_generated_in_the_body_not_supplied`
    (AST: the `org_id` bound into all three INSERTs is assigned from
    `uuid.uuid4()` inside the method) and by the second half of (A)'s
    behavioural row, which passes the SAME name and SAME email twice and still
    gets a fresh tenant rather than a refusal or a reuse.
    VACUOUS AGAINST: a caller-supplied value used for something OTHER than the
    bootstrapped check — the signature row covers the parameter list itself.

(C) ONE THAT CREATES THE ADMINISTRATOR BUT WHOSE AUDIT ROW NAMES NO ACTOR OR
    THE WRONG ORGANISATION.
    Caught by `TestTheAuditRowIsHonestAboutItsMissingActor`, which pins
    org_id = the NEW organisation, resource_id = the new org_id, and asserts
    the NULL actor EXISTS and that `operating_context` compensates for it in
    the existing `details` JSONB. The NULL actor is the human's ruled and
    accepted cost, not a defect this wave may fix: there is no principal to
    name, and a `created_by` column was considered and recommended against
    for exactly that reason.
    VACUOUS AGAINST: an audit row written but never committed — the error-path
    row in `TestAFailureAtTheLastStepLeavesNothing` covers the commit.

(D) ONE WHERE A FAILURE AT THE FINAL STEP LEAVES THE ORGANISATION OR THE USER
    BEHIND.
    Caught by `TestAFailureAtTheLastStepLeavesNothing`, which reads back
    through `login` and `get_user` — domain methods, Standing Rule 2a — and
    is paired with the positive twin that the identical call without the
    injected failure creates all three rows.
    VACUOUS AGAINST: a method that creates nothing; the positive twin is the
    pairing Standing Rule 4 requires.

(E) GRANTING `'System'` TO A HUMAN PRINCIPAL.
    Caught by `TestItGrantsAdministratorAndNothingElse`: the role is proved to
    be a SQL literal with no parameter behind it (AST), the provisioned user's
    role set read back through `get_user_roles` is exactly
    `["Administrator"]`, and no `'System'` assignment exists anywhere after
    provisioning.
    VACUOUS AGAINST: a later caller granting System through `assign_role` —
    w141's own suite owns that, and this file does not restate it.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import uuid

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
DATA_ACCESS_SOURCE = CLINICAL / "data_access.py"

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"

STAFF_COOKIE = "clinical_session"

OPERATING_CONTEXT = "w153 test: operator provisioning run from a Python interpreter"


def _now() -> dt.datetime:
    """
    THE REAL WALL CLOCK, READ AFRESH EACH TIME. It used to be the module-level
    literal `dt.datetime(2026, 9, 19, 9, 0, 0, tzinfo=dt.timezone.utc)`, and
    that literal made this file's HEADLINE ROW RED FOR EVER after 09:30 UTC on
    2026-09-19.

    WHY, EXACTLY -- because the shape matters more than the one row it broke.
    `FakeClock` is injected into the `DataAccess` that CREATES the session
    (`dao.login`), so the session's `created_at`/`last_seen` were stamped at the
    frozen literal. But
    `TestTheProvisionedAdministratorCanReachAWaveTwoRoute` then drives that
    session over HTTP through `clinical.app`, which builds its OWN `DataAccess`
    on the REAL clock and validates the cookie against a 30-minute idle window.
    Two clocks, one session: the faked one wrote the timestamp and the real one
    judged it. The row could only pass while real-now was within 30 minutes of a
    fixed point in the past, so it passed on the day it was written and could
    never pass again.

    WHY A FUNCTION AND NOT A MODULE-LEVEL `dt.datetime.now(...)`. A module-level
    call is evaluated ONCE, at import, which for this suite is collection time.
    `clinical/tests` takes well over thirty minutes to run serially (measured
    this wave), so a single import-time timestamp would re-introduce exactly the
    same expiry with a longer fuse and a far worse failure mode: red only on a
    slow machine, green on a fast one. Read per `FakeClock`, the gap between the
    session's timestamp and the HTTP request is the duration of ONE test.

    THE CLOCK IS STILL FROZEN WITHIN A TEST -- `FakeClock` captures this value
    once at construction and returns it unchanged -- so determinism inside a
    test is unaffected. What changed is only WHERE the frozen point sits: on the
    real timeline instead of at a literal in the past.
    """
    return dt.datetime.now(dt.timezone.utc)


class FakeClock:
    """One clock, injected, as the layer's docstring requires."""

    def __init__(self, start: dt.datetime | None = None) -> None:
        # `if start is not None`, not `start or _now()`: an explicit epoch-zero
        # or otherwise falsey datetime should be honoured, not silently
        # replaced.
        self._now = start if start is not None else _now()

    def now(self) -> dt.datetime:
        return self._now


# ═══════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def clinical_dsn(monkeypatch):
    """A bootstrapped, empty database, prepared exactly the way w152's does.

    `clinical.bootstrap` is used HERE, for the thing it is actually for — the
    two Postgres login roles and the schema. It is not, and cannot be, the
    identity bootstrap this file is about.
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
def conn(clinical_dsn):
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    from clinical.data_access import BcryptHasher, DataAccess

    return DataAccess(conn, clock=FakeClock(), password_hasher=BcryptHasher(rounds=4))


def _rows(conn, sql, params=()):
    """Presence/absence of a row ONLY.

    Deliberate, and not a lapse from Standing Rule 2a: every assertion in this
    file about an ACCESS RULE or about STATE goes through the method under
    test (`login`, `get_user`, `get_user_roles`, `read_audit`, or an HTTP
    route). These helpers are used for the audit table, which has no
    session-free reader, and for counting rows after a rollback — where the
    question is literally "is there a row", which is the one question raw SQL
    answers better than a method whose own correctness is in doubt.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ═══════════════════════════════════════════════════════════════════════════
# 1-3. FRESH ORGANISATION · FIRST USER · FIRST ADMINISTRATOR
#      and the positive twin: it produces a WORKING administrator.
# ═══════════════════════════════════════════════════════════════════════════


class TestTheBootstrapProducesAWorkingAdministrator:
    """
    THE POSITIVE TWIN THAT EVERY REFUSAL ROW BELOW DEPENDS ON. A row asserting
    "the second attempt fails" is satisfied by a mechanism that never works at
    all, so this class runs first and proves the first attempt genuinely
    produced an administrator who can act.
    """

    def test_the_organisation_the_user_and_the_administrator_all_exist(self, dao, conn):
        org_id = dao.provision_organisation(
            "St Elsewhere NHS Trust",
            "admin@st-elsewhere.test",
            "correct horse battery",
            operating_context=OPERATING_CONTEXT,
        )
        assert isinstance(org_id, uuid.UUID)

        # Read back THROUGH THE DOMAIN METHODS. login is what proves the user
        # row is real AND usable; get_user is what proves the org predicate
        # resolves it; get_user_roles is what proves the assignment landed.
        session = dao.login("admin@st-elsewhere.test", org_id, "correct horse battery")
        assert session.org_id == org_id

        assert dao.get_user(session, session.user_id).email == "admin@st-elsewhere.test"
        assert [a.role for a in dao.get_user_roles(session, session.user_id)] == ["Administrator"]

        # The organisation is ENABLED. `disabled` takes its schema default;
        # provisioning does not touch it (set_organisation_disabled, held on
        # w128-provisioning, is its only writer and is not this wave's).
        assert _rows(conn, "SELECT disabled FROM organisations WHERE org_id = %s", (org_id,)) == [(False,)]

    def test_the_first_administrator_granted_the_role_to_themselves(self, dao, conn):
        """
        `assigned_by` is NOT NULL and there is no earlier principal in this
        organisation to name, so the new user's own id goes there — the
        precedent `_create_system_session` already set for `'System'`.
        """
        org_id = dao.provision_organisation(
            "Royal Free", "admin@royal-free.test", "pw", operating_context=OPERATING_CONTEXT
        )
        session = dao.login("admin@royal-free.test", org_id, "pw")
        assigned_by = _rows(conn, "SELECT assigned_by FROM role_assignments WHERE org_id = %s", (org_id,))
        assert assigned_by == [(session.user_id,)]

    def test_the_signing_identity_fields_are_carried_through(self, dao):
        org_id = dao.provision_organisation(
            "Guy's",
            "dr.who@guys.test",
            "pw",
            full_name="Dr Who",
            registration_number="GMC0000001",
            hospital="Guy's Hospital",
            operating_context=OPERATING_CONTEXT,
        )
        session = dao.login("dr.who@guys.test", org_id, "pw")
        user = dao.get_user(session, session.user_id)
        assert (user.full_name, user.registration_number, user.hospital) == ("Dr Who", "GMC0000001", "Guy's Hospital")

    def test_login_still_refuses_the_wrong_password(self, dao):
        """A cheap guard that provisioning did not weaken authentication."""
        from clinical.data_access import AuthenticationError

        org_id = dao.provision_organisation("Pw", "admin@pw.test", "right", operating_context=OPERATING_CONTEXT)
        with pytest.raises(AuthenticationError):
            dao.login("admin@pw.test", org_id, "wrong")
        assert dao.login("admin@pw.test", org_id, "right").org_id == org_id


class TestTheProvisionedAdministratorCanReachAWaveTwoRoute:
    """
    THE HEADLINE ROW. Wave 2 landed four administrative routes at `fd9d63b`
    and recorded them as IMPLEMENTED + UNREACHABLE, because nothing in
    production could make the Administrator they all require. This class is
    the measurement that the classification has changed: the administrator
    this bootstrap produced logs in and drives
    `POST /admin/users/{user_id}/roles` to a 201 over HTTP, through
    `clinical.app` — the app the service actually serves, not a test-local
    stand-in.

    NOTHING HERE IS FIXTURE-PROVISIONED. w152's own suite inserts the role row
    directly and says so; this class does not, and that difference is the
    whole point of the wave.
    """

    def test_the_provisioned_administrator_drives_a_wave_two_route_to_success(self, dao, clinical_dsn):
        from fastapi.testclient import TestClient

        from clinical.app import app

        org_id = dao.provision_organisation(
            "Addenbrooke's", "admin@addenbrookes.test", "pw", operating_context=OPERATING_CONTEXT
        )
        session = dao.login("admin@addenbrookes.test", org_id, "pw")
        colleague = dao.create_user(org_id, "tech@addenbrookes.test", "pw2")

        client = TestClient(app)
        response = client.post(
            f"/admin/users/{colleague}/roles",
            json={"role": "Lab technician"},
            cookies={STAFF_COOKIE: str(session.session_id)},
        )

        assert response.status_code == 201, response.content
        assert uuid.UUID(response.json()["assignment_id"])

        # Read the effect back through the domain method, not through the
        # response body the route just wrote (Standing Rule 2a).
        assert [a.role for a in dao.get_user_roles(session, colleague)] == ["Lab technician"]

    def test_the_same_route_refuses_an_unauthenticated_caller(self, dao, clinical_dsn):
        """
        THE NON-VACUITY TWIN for the row above: without it, a 201 would be
        equally consistent with a route that authorises nobody. The
        authorisation is still on, and it is the SESSION that made the
        difference.
        """
        from fastapi.testclient import TestClient

        from clinical.app import app

        org_id = dao.provision_organisation("Anon", "admin@anon.test", "pw", operating_context=OPERATING_CONTEXT)
        colleague = dao.create_user(org_id, "tech@anon.test", "pw2")

        response = TestClient(app).post(f"/admin/users/{colleague}/roles", json={"role": "Lab technician"})
        assert response.status_code == 401, response.content


# ═══════════════════════════════════════════════════════════════════════════
# 4. FAILURE ROLLS BACK COMPLETELY — wrong-first (D)
# ═══════════════════════════════════════════════════════════════════════════


class TestAFailureAtTheLastStepLeavesNothing:
    """
    THE TRANSACTION BOUNDARY IS `@transactional` ON THIS METHOD, and the three
    writes are open-coded inside it precisely so that no inner `@auditable`
    commit can land between them. Force the LAST step to fail and prove
    nothing survives — read back through the domain methods.
    """

    @staticmethod
    def _break_the_role_step(monkeypatch):
        from clinical.data_access import DataAccess

        real_execute = DataAccess._execute

        def failing_execute(self, sql, params=()):
            if "INSERT INTO role_assignments" in sql:
                raise RuntimeError("simulated failure at the role-assignment step")
            return real_execute(self, sql, params)

        monkeypatch.setattr(DataAccess, "_execute", failing_execute)

    def test_nothing_survives_a_failure_at_the_role_step(self, dao, conn, monkeypatch):
        from clinical.data_access import AuthenticationError

        self._break_the_role_step(monkeypatch)
        with pytest.raises(RuntimeError, match="simulated failure at the role-assignment step"):
            dao.provision_organisation("Half A Hospital", "admin@half.test", "pw", operating_context=OPERATING_CONTEXT)
        monkeypatch.undo()

        # THROUGH THE DOMAIN METHOD. There is no organisation to log into, so
        # login refuses — and it refuses for every candidate org_id, because
        # none exists. The raw counts below are the "is there a row at all"
        # question, which is the one thing a method cannot answer when the
        # method needs a session that provisioning never produced.
        assert _rows(conn, "SELECT org_id FROM organisations WHERE name = %s", ("Half A Hospital",)) == []
        assert _rows(conn, "SELECT user_id FROM users WHERE email = %s", ("admin@half.test",)) == []
        assert _rows(conn, "SELECT assignment_id FROM role_assignments") == []

        with pytest.raises(AuthenticationError):
            dao.login("admin@half.test", uuid.uuid4(), "pw")

    def test_the_identical_call_without_the_injected_failure_creates_all_three(self, dao, conn):
        """
        THE POSITIVE TWIN. Without it the three absence assertions above are
        satisfied just as happily by a method that creates nothing at all —
        Standing Rule 4.
        """
        org_id = dao.provision_organisation(
            "Half A Hospital", "admin@half.test", "pw", operating_context=OPERATING_CONTEXT
        )
        session = dao.login("admin@half.test", org_id, "pw")
        assert dao.get_user(session, session.user_id).email == "admin@half.test"
        assert [a.role for a in dao.get_user_roles(session, session.user_id)] == ["Administrator"]

        assert len(_rows(conn, "SELECT org_id FROM organisations WHERE name = %s", ("Half A Hospital",))) == 1
        assert len(_rows(conn, "SELECT user_id FROM users WHERE email = %s", ("admin@half.test",))) == 1
        assert len(_rows(conn, "SELECT assignment_id FROM role_assignments")) == 1

    def test_rolling_the_work_back_does_not_roll_the_evidence_back(self, dao, conn, monkeypatch):
        """
        `@auditable` is the OUTER decorator, so its error branch writes and
        commits AFTER `@transactional` has already rolled the data back. The
        attempt is recorded even though nothing survives it — and this is also
        the non-vacuity twin for wrong-first (C): it proves the audit write
        actually commits.
        """
        self._break_the_role_step(monkeypatch)
        with pytest.raises(RuntimeError):
            dao.provision_organisation("Ghost", "admin@ghost.test", "pw", operating_context=OPERATING_CONTEXT)
        monkeypatch.undo()

        rows = _rows(
            conn,
            "SELECT outcome, details FROM audit_log WHERE action = %s ORDER BY log_id",
            ("organisation_provisioning_failed",),
        )
        assert [r[0] for r in rows] == ["error"]
        assert rows[0][1]["operating_context"] == OPERATING_CONTEXT
        assert rows[0][1]["name"] == "Ghost"


# ═══════════════════════════════════════════════════════════════════════════
# 5. AUDIT ATTRIBUTION — wrong-first (C)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheAuditRowIsHonestAboutItsMissingActor:
    def test_it_names_the_new_organisation_records_a_null_actor_and_carries_the_context(self, dao, conn):
        org_id = dao.provision_organisation("Audited", "admin@audited.test", "pw", operating_context=OPERATING_CONTEXT)

        rows = _rows(
            conn,
            "SELECT org_id, user_id, actor_role, resource_type, resource_id, outcome, details "
            "FROM audit_log WHERE action = %s ORDER BY log_id",
            ("organisation_provisioned",),
        )
        assert len(rows) == 1
        recorded_org, user_id, actor_role, resource_type, resource_id, outcome, details = rows[0]

        # THE ORGANISATION IS THE NEW ONE. A row filed under NULL would be
        # invisible to `read_audit`, which is Auditor-gated and filters
        # WHERE org_id = %s — the w137 defect, in this exact shape.
        assert recorded_org == org_id
        assert resource_type == "organisation"
        assert resource_id == str(org_id)
        assert outcome == "success"

        # THE HOLE, ASSERTED RATHER THAN LEFT TO BE DISCOVERED, and it is the
        # human's ruled and accepted cost: the caller is an operator outside
        # the tenancy model and no principal exists to name. A `created_by`
        # column was considered in the w128 ruling and recommended against
        # for exactly that reason — it could not be populated truthfully.
        assert user_id is None
        assert actor_role is None

        # ...and the compensation for it, in the EXISTING details JSONB.
        assert details["operating_context"] == OPERATING_CONTEXT
        assert details["name"] == "Audited"
        assert details["admin_email"] == "admin@audited.test"

    def test_the_provisioned_administrators_own_organisation_can_read_the_row(self, dao):
        """
        THE POSITIVE TWIN FOR "org_id IS THE NEW ORGANISATION", and it is the
        assertion that a raw `SELECT org_id` cannot make: the row is filed
        where the organisation that owns it can actually look. Read through
        `read_audit`, the Auditor-gated domain method, not through SQL.
        """
        org_id = dao.provision_organisation(
            "Readable", "admin@readable.test", "pw", operating_context=OPERATING_CONTEXT
        )
        session = dao.login("admin@readable.test", org_id, "pw")
        # The administrator this bootstrap made grants itself the Auditor role
        # through the Administrator-gated `assign_role` — which is itself a
        # second demonstration that the bootstrap cycle is broken.
        dao.assign_role(session, session.user_id, "Auditor")

        # (log_id, org_id, user_id, action, details, ip_address, timestamp)
        entries = dao.read_audit(session, actions=["organisation_provisioned"])
        assert [e[3] for e in entries] == ["organisation_provisioned"]
        assert [e[1] for e in entries] == [org_id]

        # The non-vacuity twin: the SAME reader, unfiltered, sees more than
        # this one action — so the single-element result above is the filter
        # working, not the reader returning nothing.
        assert len(dao.read_audit(session)) > 1

    def test_the_password_never_reaches_the_audit_details(self, dao, conn):
        dao.provision_organisation(
            "Secrets", "admin@secrets.test", "hunter2-do-not-log-me", operating_context=OPERATING_CONTEXT
        )
        blob = json.dumps([r[0] for r in _rows(conn, "SELECT details FROM audit_log")], default=str)
        assert "hunter2-do-not-log-me" not in blob


# ═══════════════════════════════════════════════════════════════════════════
# 6. CANNOT BE REPEATED TO CREATE UNAUTHORIZED ADMINISTRATORS
#    — wrong-first (A) and (B), behaviourally
# ═══════════════════════════════════════════════════════════════════════════


class TestItCannotBeRepeatedAgainstAnOrganisationThatAlreadyHasAnAdministrator:
    """
    THE HONEST STATEMENT OF WHAT IS AND IS NOT REFUSED.

    A second call is NOT refused — it provisions a SECOND organisation, which
    is the spec's single-instance multi-organisation tenancy (§4.2) and not a
    bypass. What cannot happen, and is what "unauthorized Administrator"
    means, is a second Administrator appearing in an organisation that already
    has one. That is refused by the ABSENCE OF ANY PARAMETER naming an
    existing organisation, and it is proved here from the database.
    """

    def test_a_second_call_with_the_same_name_and_email_cannot_touch_the_first_organisation(self, dao, conn):
        from clinical.data_access import AuthenticationError

        org_a = dao.provision_organisation("Same Name", "admin@same.test", "pw-a", operating_context=OPERATING_CONTEXT)
        session_a = dao.login("admin@same.test", org_a, "pw-a")
        admin_a = session_a.user_id

        # The most hostile repetition available: identical name, identical
        # administrator email, different password.
        org_b = dao.provision_organisation("Same Name", "admin@same.test", "pw-b", operating_context=OPERATING_CONTEXT)
        assert org_b != org_a

        # ORGANISATION A IS UNTOUCHED. Read through the domain method.
        roles_a = [a.role for a in dao.get_user_roles(session_a, admin_a)]
        assert roles_a == ["Administrator"]
        assert _rows(
            conn,
            "SELECT user_id FROM role_assignments WHERE org_id = %s AND revoked_at IS NULL",
            (org_a,),
        ) == [(admin_a,)]

        # THE SECOND CREDENTIAL IS NOT A CREDENTIAL IN ORGANISATION A. This is
        # the escalation attempt in concrete form and it fails at login.
        with pytest.raises(AuthenticationError):
            dao.login("admin@same.test", org_a, "pw-b")

        # ...and the positive twin, so the refusal above is org scoping rather
        # than a broken password: the same credential works in B.
        assert dao.login("admin@same.test", org_b, "pw-b").org_id == org_b

    def test_the_second_administrator_cannot_act_on_the_first_organisation(self, dao):
        """
        Escalation proved absent at the CAPABILITY level, not merely at the
        row level: B's administrator, holding a real live Administrator
        session, cannot read or assign inside A.
        """
        from clinical.data_access import NotFoundError

        org_a = dao.provision_organisation("A", "admin@a.test", "pw", operating_context=OPERATING_CONTEXT)
        org_b = dao.provision_organisation("B", "admin@b.test", "pw", operating_context=OPERATING_CONTEXT)

        session_a = dao.login("admin@a.test", org_a, "pw")
        session_b = dao.login("admin@b.test", org_b, "pw")

        with pytest.raises(NotFoundError):
            dao.get_user(session_b, session_a.user_id)
        with pytest.raises(NotFoundError):
            dao.assign_role(session_b, session_a.user_id, "Lab technician")

        # The positive twin: B's administrator is genuinely an Administrator,
        # so the refusals above are organisation scoping and not a missing
        # role.
        colleague_b = dao.create_user(org_b, "tech@b.test", "pw2")
        assert isinstance(dao.assign_role(session_b, colleague_b, "Lab technician"), uuid.UUID)

        # And A still has exactly one Administrator afterwards.
        assert [a.role for a in dao.get_user_roles(session_a, session_a.user_id)] == ["Administrator"]


# ═══════════════════════════════════════════════════════════════════════════
# WHAT MAKES IT INERT — structural, from the syntax tree, never from a flag
# ═══════════════════════════════════════════════════════════════════════════


def _provision_organisation_ast():
    tree = ast.parse(DATA_ACCESS_SOURCE.read_text(encoding="utf-8"))
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == "provision_organisation":
                return node
    raise AssertionError("provision_organisation is not a method of DataAccess")


def _declared_parameters():
    """
    The parameter list AS WRITTEN IN THE SOURCE, read from the syntax tree.

    IT CANNOT COME FROM `inspect.signature`, AND THIS IS A MEASURED DEFECT
    RATHER THAN A PREFERENCE. `@auditable`'s wrapper is defined as
    `def wrapper(self, *args, **kwargs)` and — unlike `@transactional`'s — it
    carries NO `functools.wraps`, so it exposes no `__wrapped__` for
    `inspect.signature` to follow. Measured at this tree:

        inspect.signature(DataAccess.provision_organisation) -> (self, *args, **kwargs)
        inspect.signature(DataAccess.assign_role)            -> (self, *args, **kwargs)

    — for EVERY `@auditable` method. A signature row written the obvious way
    therefore asserts that three names are absent from a three-name list that
    never contains them, and passes against every wrong version there is. It
    was written that way here first and the wrong-first exercise caught it:
    the `org_id`-accepting bootstrap passed
    `test_it_accepts_no_organisation_identifier_of_any_kind` outright. That is
    the Phase 5d cannot-fail shape in a new place, and
    `test_the_parameter_reader_sees_the_real_parameters` is the non-vacuity
    twin that keeps it fixed.
    """
    node = _provision_organisation_ast()
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    annotations = {
        a.arg: ast.unparse(a.annotation) if a.annotation is not None else None
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    }
    return names, annotations


class TestTheShapeThatMakesItInert:
    def test_the_parameter_reader_sees_the_real_parameters(self):
        """
        STANDING RULE 4, AND THE ROW THAT MAKES THE THREE BELOW MEAN ANYTHING.
        Without it, an extractor that returned an empty list would satisfy
        every absence assertion in this class — which is exactly what
        `inspect.signature` silently did here.
        """
        names, annotations = _declared_parameters()
        assert names == [
            "self",
            "name",
            "admin_email",
            "admin_password",
            "operating_context",
            "full_name",
            "registration_number",
            "hospital",
        ], names
        assert annotations["operating_context"] == "str"
        assert annotations["full_name"] == "Optional[str]"

    def test_it_takes_no_session(self):
        """
        It cannot: a session in this organisation is precisely what does not
        exist yet. Pinned so a later change cannot quietly gate it on one and
        re-close the cycle this method opened.
        """
        names, annotations = _declared_parameters()
        assert "session" not in names
        assert "Session" not in [a for a in annotations.values() if a]

    def test_it_accepts_no_organisation_identifier_of_any_kind(self):
        """
        WRONG-FIRST (A) AND (B), STRUCTURALLY. The bootstrap that can run again
        against a live tenant is the one that accepts an organisation id; the
        one whose already-bootstrapped check reads a caller value is the one
        that accepts a flag. Neither parameter exists, so neither version is
        writable — the house defence-by-absent-parameter pattern.
        """
        names, annotations = _declared_parameters()

        assert "org_id" not in names
        assert not [n for n in names if "org" in n.lower() and n != "operating_context"], names
        # No UUID-typed parameter under any spelling: an organisation id
        # smuggled in under another name would still be an organisation id.
        assert not [n for n, a in annotations.items() if a and "UUID" in a], annotations
        # No boolean parameter: no `already_bootstrapped`, no `force`, no
        # `reuse_existing` — nothing a caller could set to re-open the path.
        assert not [n for n, a in annotations.items() if a and "bool" in a], annotations

    def test_the_organisation_id_is_generated_in_the_body_not_supplied(self):
        """
        WRONG-FIRST (B), at the value rather than the signature: `org_id` is
        assigned from `uuid.uuid4()` inside the method, so there is no
        caller-supplied value for an already-bootstrapped check to read.
        """
        target = _provision_organisation_ast()
        generated = {
            node.targets[0].id
            for node in ast.walk(target)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "uuid4"
        }
        assert "org_id" in generated, sorted(generated)

    def test_it_composes_none_of_the_decorated_creators(self):
        """
        THE ATOMICITY TRAP, pinned in source as well as in behaviour. The
        behavioural row is the real guard; this one names the cause, so a
        reader who reintroduces the composition is told WHY rather than only
        that a transaction test broke.
        """
        target = _provision_organisation_ast()
        called = {
            node.func.attr
            for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        forbidden = called & {"create_organisation", "create_user", "assign_role"}
        assert not forbidden, (
            f"provision_organisation calls {sorted(forbidden)}, which are @auditable. "
            "@auditable commits after writing its audit row, so each inner call would "
            "commit mid-way and the three writes would stop being one transaction."
        )

    def test_the_analyser_can_see_a_composition_when_one_exists(self):
        """
        Standing Rule 4: the non-vacuity twin for the two AST rows above. A
        walker that found nothing would satisfy both.
        """
        probe = ast.parse(
            "def provision_organisation(self):\n"
            "    org_id = self.create_organisation('x')\n"
            "    self.create_user(org_id, 'e', 'p')\n"
        ).body[0]
        called = {
            node.func.attr
            for node in ast.walk(probe)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called & {"create_organisation", "create_user"} == {"create_organisation", "create_user"}

    def test_it_is_reachable_from_no_served_route(self):
        """
        THE THIRD INERTNESS FACT. The w128 ruling says NO HTTP ROUTE and this
        wave does not change that: the only caller is an operator who already
        holds the database credential and could therefore already write these
        three rows by hand. A route onto this method would be the arbitrary
        permanent bypass the dispatch forbids.

        Enumerated by WALKING the route tree (Standing Rule 9, in its
        corrected form — `app.routes` returns opaque wrappers on this stack),
        with a non-vacuity check that refuses both the empty and the
        wrapper-shaped answer.
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        paths = {path for path, _ in served_routes(app)}
        # Non-vacuity, in the corrected Rule 9 form: the walk must see the real
        # mounted surface, not the five opaque wrappers `app.routes` returns on
        # this stack, or this row proves nothing at all.
        assert len(paths) > 10, sorted(paths)
        assert "/admin/audit" in paths and "/auth/login" in paths, sorted(paths)

        # No module that defines any served route CALLS the method. Every
        # route this app serves is defined in one of these five (the census
        # row above is what keeps that list honest) and the handlers are thin
        # translations, so a call there is the only way a route could reach it.
        #
        # THE INSTRUMENT CHANGED AT w155, AND THE PROPERTY DID NOT.
        # This was `assert "provision_organisation" not in source` — a
        # SUBSTRING scan over the whole file, docstrings included. That is the
        # defect this repository already has a name for: grep credits prose as
        # a caller, which is how `_release_report` came to be recorded with
        # thirteen callers and zero calls. It bit here the moment w155's
        # module docstring in `admin_endpoints.py` carried the honest caveat
        # this very row exists to state — that every administrative route is
        # exactly as reachable as an operator invoking `provision_organisation`
        # directly. A guard that forbids naming the problem is not a guard.
        #
        # So the scan is now an AST ATTRIBUTE-AND-CALL census, which is
        # STRICTLY STRONGER on code and silent on prose: it catches
        # `dao.provision_organisation(...)`, a bare reference handed to
        # `Depends`, and an alias assignment — none of which a substring
        # search distinguishes from a sentence.
        def _referenced(path):
            tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names.update(alias.name for alias in node.names)
            return names

        for module_name in (
            "clinical.app",
            "clinical.endpoints",
            "clinical.admin_endpoints",
            "clinical.portal_endpoints",
            "clinical.auth_endpoints",
            # w157 (WAVE 7). THE SIXTH. This list is the "every route this app
            # serves is defined in one of these" claim above, and that claim is
            # a CENSUS -- so it grows with the surface or it silently stops
            # covering it. `clinical/workflow_endpoints.py` defines the two
            # laboratory bench routes; adding it here would NOT have failed
            # this row, which is exactly why it had to be added deliberately:
            # a module missing from this tuple is not scanned at all, and an
            # unscanned module is where a `provision_organisation` call would
            # go unnoticed.
            "clinical.workflow_endpoints",
            # w162 (THE CLINIC SURFACE). THE SEVENTH, added for the reason the
            # note above gives: this tuple IS the "every route this app serves
            # is defined in one of these" claim, and a module missing from it
            # is not scanned at all. `clinical/clinic_endpoints.py` defines the
            # three clinician routes -- registration, consent, withdrawal.
            "clinical.clinic_endpoints",
            # Stage 3. THE EIGHTH: `clinical/review_endpoints.py` defines the
            # five review routes. Same reason as every entry above -- a module
            # missing from this tuple is not scanned at all.
            "clinical.review_endpoints",
        ):
            try:
                module = __import__(module_name, fromlist=["*"])
            except ImportError:
                continue
            assert "provision_organisation" not in _referenced(module.__file__), module_name

        # The non-vacuity twin for the scan: it CAN find a domain method name
        # in those files when one is there — and, added at w155, it can tell a
        # real reference from a mention in prose, which is the whole reason
        # the instrument changed.
        from clinical import admin_endpoints

        referenced = _referenced(admin_endpoints.__file__)
        assert "assign_role" in referenced
        assert "enrol_patient_login" in referenced
        assert "provision_organisation" in pathlib.Path(admin_endpoints.__file__).read_text(encoding="utf-8"), (
            "this file no longer MENTIONS provision_organisation, so the row above can no longer "
            "distinguish a prose mention from a call -- re-check that it still can"
        )


class TestItGrantsAdministratorAndNothingElse:
    """WRONG-FIRST (E): granting `'System'` to a human principal."""

    def test_the_role_is_a_sql_literal_with_no_parameter_behind_it(self):
        names, _ = _declared_parameters()
        assert "role" not in names

        target = _provision_organisation_ast()
        literals = [
            node.value
            for node in ast.walk(target)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "role_assignments" in node.value
        ]
        assert literals, "no INSERT INTO role_assignments statement found in provision_organisation"
        assert all("'Administrator'" in sql for sql in literals), literals
        assert not any("System" in sql for sql in literals), literals

    def test_the_provisioned_user_holds_administrator_and_nothing_else(self, dao, conn):
        org_id = dao.provision_organisation("Lane", "admin@lane.test", "pw", operating_context=OPERATING_CONTEXT)
        session = dao.login("admin@lane.test", org_id, "pw")

        assert [a.role for a in dao.get_user_roles(session, session.user_id)] == ["Administrator"]
        assert _rows(conn, "SELECT role FROM role_assignments WHERE role = %s", ("System",)) == []

        # The positive twin for the absence above: the reader CAN see a System
        # row when one exists, so "no System row" is a measurement rather than
        # a query that never matches.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'System', %s, %s)",
                # `assigned_at` only, on a row this test inserts and then rolls
                # back. Nothing reads it, nothing orders on it and nothing
                # asserts it, so any instant does; it reads the same clock as
                # the rest of the file rather than carrying a second literal.
                (session.user_id, org_id, session.user_id, _now()),
            )
        assert _rows(conn, "SELECT role FROM role_assignments WHERE role = %s", ("System",)) == [("System",)]
        conn.rollback()

    def test_no_approver_is_granted_and_no_retention_policy_is_seeded(self, dao, conn):
        """
        Both from the w128 ruling: Approver needs a qualification basis this
        method has no business inventing, and `_resolve_vcf_retention_days`
        LEFT JOINs and falls back to the code default, so absence is a valid
        and already-tested state.
        """
        org_id = dao.provision_organisation("Lane2", "admin@lane2.test", "pw", operating_context=OPERATING_CONTEXT)
        roles = {r[0] for r in _rows(conn, "SELECT role FROM role_assignments WHERE org_id = %s", (org_id,))}
        assert roles == {"Administrator"}
        assert _rows(conn, "SELECT artefact_class FROM retention_policies WHERE org_id = %s", (org_id,)) == []


# ═══════════════════════════════════════════════════════════════════════════
# clinical/bootstrap.py IS NOT THIS MECHANISM — pinned, so the filename
# cannot be mistaken for the model a second time.
# ═══════════════════════════════════════════════════════════════════════════


def test_clinical_bootstrap_is_a_database_bootstrap_not_an_identity_one():
    """
    The wave's first question, kept as a test rather than as a paragraph
    somebody can stop believing. `clinical/bootstrap.py` creates POSTGRES
    LOGIN ROLES and applies `schema.sql`. It writes no application row of any
    kind, and a later change that made it write one would be a new bootstrap
    model needing its own ruling.
    """
    import clinical.bootstrap as bootstrap

    source = pathlib.Path(bootstrap.__file__).read_text(encoding="utf-8")
    for table in ("organisations", "users", "role_assignments"):
        assert f"INSERT INTO {table}" not in source, table
    assert "Administrator" not in source
    # What it DOES do, so the absences above are a measurement of a real file.
    assert "CREATE ROLE" in source and "ALTER ROLE" in source
    assert bootstrap.APP_ROLE == "clinical_app"
    assert bootstrap.RETENTION_ROLE == "clinical_retention"
