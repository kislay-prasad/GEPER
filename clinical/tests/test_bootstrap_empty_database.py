"""
clinical/tests/test_bootstrap_empty_database.py
───────────────────────────────────────────────
Does ``schema.sql`` apply to a GENUINELY empty database?

Until ``clinical/bootstrap.py`` existed it did not. The schema grants to
``clinical_app`` and ``clinical_retention`` while its only ``CREATE ROLE``
lines are comments, so the first ``GRANT USAGE ON SCHEMA public TO
clinical_app`` failed on any database where nobody had made the roles by hand.

THIS FILE DELIBERATELY DOES NOT USE THE ``conn`` FIXTURE FROM
``test_clinical_domain.py``, AND THAT IS THE WHOLE POINT. That fixture runs

    DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object ... END $$;

before applying the schema, which is exactly why the defect went unseen for
the life of the file. A test that reused it would pass no matter what this
module does. So ``empty_database`` below drops the roles as well as the
schema, and every test here starts from a database with neither.

It also drops them WITH ``DROP OWNED BY``: a role that has been granted
anything cannot be dropped while those grants exist, and after a previous run
of the suite it will have them.

The three claims are kept apart on purpose, because the old fixture passed the
first while quietly failing the third:
  1. the schema APPLIES               -- test_schema_applies_after_bootstrap
  2. without the bootstrap it does NOT -- test_schema_fails_on_empty_database_without_bootstrap
     (the negative control: if this passed, test 1 would be proving nothing)
  3. the roles can be CONNECTED AS     -- test_bootstrapped_app_role_can_actually_connect
     ("the schema applied" and "a service could log in" are different claims;
      the fixture's LOGIN-less, password-less roles satisfied the first only)
"""

import os
import pathlib

import pytest

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

# Passwords for the roles this test creates. They are test-local values for a
# throwaway database and are not credentials for anything: the point of the
# test is that a password must be SUPPLIED, not that this particular one is
# secret. Production passwords come from the operator's environment -- see
# clinical/bootstrap.py's docstring.
APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"


def _drop_role_if_exists(cur, role: str) -> None:
    from psycopg import sql

    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if cur.fetchone() is None:
        return
    # Grants held by the role block DROP ROLE; DROP OWNED BY removes both the
    # role's owned objects and the privileges granted to it.
    cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture()
def empty_database():
    """
    A database with no schema objects AND NO clinical roles.

    The second half is what distinguishes this from the suite's existing
    ``conn`` fixture, and it is the reason this file can see the defect that
    one cannot.
    """
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")

    from clinical.bootstrap import APP_ROLE, RETENTION_ROLE

    connection = psycopg.connect(DSN, autocommit=True, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        _drop_role_if_exists(cur, APP_ROLE)
        _drop_role_if_exists(cur, RETENTION_ROLE)
    connection.close()

    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


def _roles_present(connection, role: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        return cur.fetchone() is not None


class TestSchemaAppliesToAnEmptyDatabase:
    def test_schema_fails_on_empty_database_without_bootstrap(self, empty_database):
        """
        NEGATIVE CONTROL. Without the bootstrap, applying the schema to a
        database with no roles must fail -- and fail at a GRANT naming a role
        that does not exist, not merely fail somehow. If this ever passes, the
        acceptance test below is proving nothing and both should be re-read.
        """
        import psycopg

        from clinical.bootstrap import APP_ROLE

        assert not _roles_present(empty_database, APP_ROLE), "fixture did not give us an empty database"

        with pytest.raises(psycopg.errors.UndefinedObject) as excinfo:
            with empty_database.cursor() as cur:
                cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        empty_database.rollback()

        assert APP_ROLE in str(excinfo.value), (
            f"the schema failed, but not for the reason this test is about: {excinfo.value}"
        )

    def test_schema_applies_after_bootstrap(self, empty_database):
        """ACCEPTANCE. Empty database in, applied schema out, one bootstrap between."""
        from clinical.bootstrap import APP_ROLE, RETENTION_ROLE, apply_schema, ensure_roles

        outcome = ensure_roles(
            empty_database,
            app_password=APP_PASSWORD,
            retention_password=RETENTION_PASSWORD,
        )
        assert outcome == {APP_ROLE: "created", RETENTION_ROLE: "created"}

        apply_schema(empty_database)
        empty_database.commit()

        # The schema really is there, not merely un-errored.
        with empty_database.cursor() as cur:
            cur.execute("SELECT to_regclass('public.organisations'), to_regclass('public.audit_log')")
            organisations, audit_log = cur.fetchone()
        assert organisations is not None
        assert audit_log is not None

    def test_ensure_roles_is_idempotent_and_repairs_a_loginless_role(self, empty_database):
        """
        Second run reports ``updated`` rather than failing on the duplicate.

        And the case that is not merely a re-run: a role created the way the
        existing ``conn`` fixture creates them -- no LOGIN, no password -- is
        converged into one that can authenticate, rather than being skipped as
        "already exists".
        """
        from psycopg import sql

        from clinical.bootstrap import APP_ROLE, RETENTION_ROLE, ensure_roles

        with empty_database.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(APP_ROLE)))
        with empty_database.cursor() as cur:
            cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            assert cur.fetchone()[0] is False, "precondition: the fixture-style role cannot log in"

        outcome = ensure_roles(
            empty_database,
            app_password=APP_PASSWORD,
            retention_password=RETENTION_PASSWORD,
        )
        assert outcome[APP_ROLE] == "updated"
        assert outcome[RETENTION_ROLE] == "created"

        with empty_database.cursor() as cur:
            cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            assert cur.fetchone()[0] is True, "a pre-existing role was left unable to log in"
        empty_database.commit()

    def test_ensure_roles_refuses_an_empty_password(self, empty_database):
        """A role that cannot authenticate is the failure this file exists to end."""
        from clinical.bootstrap import ensure_roles

        with pytest.raises(ValueError, match="cannot authenticate"):
            ensure_roles(empty_database, app_password="", retention_password=RETENTION_PASSWORD)
        empty_database.rollback()


class TestTheRolesCanActuallyBeConnectedAs:
    """
    'The schema applied' and 'a service could log in as this role' are
    different claims. The suite's existing fixture satisfied the first with
    roles that failed the second, which is what made the gap invisible.
    """

    def test_bootstrapped_app_role_can_actually_connect(self, empty_database):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from clinical.bootstrap import APP_ROLE, apply_schema, ensure_roles

        ensure_roles(
            empty_database,
            app_password=APP_PASSWORD,
            retention_password=RETENTION_PASSWORD,
        )
        apply_schema(empty_database)
        empty_database.commit()

        params = conninfo_to_dict(DSN)
        params["user"] = APP_ROLE
        params["password"] = APP_PASSWORD
        as_app = psycopg.connect(make_conninfo(**params), connect_timeout=10)
        try:
            with as_app.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == APP_ROLE
                # Not just a login: the grants schema.sql makes to this role
                # are usable by it. SELECT on audit_log is granted.
                cur.execute("SELECT count(*) FROM audit_log")
                assert cur.fetchone()[0] == 0
        finally:
            as_app.close()
