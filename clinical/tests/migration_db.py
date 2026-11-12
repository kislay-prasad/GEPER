"""
clinical/tests/migration_db.py
──────────────────────────────
THE MIGRATION TEST DATABASE LIFECYCLE, and it is a different lifecycle from
every other database test in this package ON PURPOSE.

THE TRAP THIS FILE EXISTS TO CLOSE. `clinical/tests/db_guard.py::reset_schema`
and its twenty-six inlined copies begin with `DROP SCHEMA public CASCADE;
CREATE SCHEMA public;` and then load `schema.sql`. A migration test that asks
one of those fixtures for a database is handed a FRESHLY CREATED one, built
from the current `schema.sql` a few lines earlier -- so every assertion about
"the upgrade worked" passes for the wrong reason, and would keep passing
against a runner that does nothing at all. That is the house pattern, in
twenty-six files, so the accident is the DEFAULT and avoiding it by
remembering to is not good enough.

SO THESE TESTS NEVER TOUCH THE SHARED SCHEMA. They CREATE THEIR OWN DATABASE
on the same server, run the whole scenario inside it, and DROP the database at
the end. Nothing here calls `reset_schema`, nothing here executes
`DROP SCHEMA public CASCADE`, and the existing fixtures are left exactly as
they are -- this is a path BESIDE them, not a replacement for them.

AND THE ISOLATION IS NOT THE PROOF EITHER. A test can still be handed a
brand-new database by accident. The structural proof is the SENTINEL: before
an upgrade, a caller writes a marker that a drop-and-recreate cannot carry
across -- `plant_sentinel` below -- and asserts afterwards that it is still
there, BY CONTENT. A dropped and recreated database loses it; there is no way
to fake surviving one. Paired, always, with the opposite assertion that the
new object the migration was for is actually present, because a marker-only
test passes against a runner that does nothing.

THE DISPOSABLE GUARD IS UNTOUCHED AND UNWEAKENED. `clinical/tests/conftest.py`
still refuses the whole session without `CLINICAL_TEST_DB_DISPOSABLE=1` before
any connection is opened; these helpers run after that gate like everything
else. They create and drop databases OF THEIR OWN on that server and never
drop the one the DSN names.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from typing import Iterator

import pytest

DSN_ENV = "CLINICAL_TEST_DSN"

# The two roles clinical/schema.sql grants to. Same statements db_guard uses,
# and the same caveat: NOLOGIN and no password, because these tests check
# grants through has_table_privilege() rather than by connecting.
_CREATE_ROLES = (
    "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
)


def require_psycopg():
    return pytest.importorskip("psycopg", reason="psycopg not installed")


def require_dsn() -> str:
    dsn = os.getenv(DSN_ENV)
    if not dsn:
        pytest.skip(f"{DSN_ENV} not set")
    return dsn


def _swap_dbname(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parsed = conninfo_to_dict(dsn)
    parsed["dbname"] = dbname
    return make_conninfo(**parsed)


@contextlib.contextmanager
def scratch_database(prefix: str = "w126m") -> Iterator[str]:
    """
    A brand-new, empty Postgres DATABASE for the duration of one test, then
    gone. Yields its DSN.

    A database rather than a schema because the thing under test creates
    tables, grants and a history table in `public`, and because two of these
    running side by side is exactly what the equivalence proof needs.

    `DROP DATABASE ... WITH (FORCE)` so a connection a test forgot to close
    cannot leave a database behind on a shared CI server. Requires PostgreSQL
    13 or later; this repository's own refusal message and CI service both
    name postgres:16.
    """
    psycopg = require_psycopg()
    dsn = require_dsn()
    name = f"{prefix}_{secrets.token_hex(6)}"

    admin = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    try:
        with admin.cursor() as cur:
            for statement in _CREATE_ROLES:
                cur.execute(statement)
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()

    try:
        yield _swap_dbname(dsn, name)
    finally:
        admin = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            admin.close()


@contextlib.contextmanager
def connect(dsn: str, *, autocommit: bool = False):
    psycopg = require_psycopg()
    connection = psycopg.connect(dsn, autocommit=autocommit, connect_timeout=10)
    try:
        yield connection
    finally:
        connection.close()


# ─── the operator's pre-build of the consents index (migration 0011, Stage 1B) ──
# Migration 0011 does not build `consents_patient_scope_latest` on a table that holds rows: the OPERATOR
# builds it first, CONCURRENTLY, and the migration adopts it after validating it. This is that step, for tests
# that upgrade a database holding consents past 0011 (docs/CLINICAL_DEPLOYMENT.md, "Upgrading to migration
# 0011"). CONCURRENTLY cannot run inside a transaction, hence autocommit. Idempotent.

CONSENTS_INDEX_NAME = "consents_patient_scope_latest"
CONSENTS_INDEX_COLUMNS = "(org_id, patient_id, scope, recorded_at DESC, consent_id DESC)"


def prebuild_consents_index(dsn: str) -> None:
    with connect(dsn, autocommit=True) as connection:
        connection.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {CONSENTS_INDEX_NAME} ON consents {CONSENTS_INDEX_COLUMNS}"
        )


# ─── the sentinel ───────────────────────────────────────────────────────────
# A marker a DROP cannot carry across. TWO of them, because they fail
# differently:
#
#   * an `organisations` row -- REAL clinical data in a REAL table the
#     migration set does not touch. Its survival is the property an operator
#     actually cares about.
#   * a table of its own -- which also survives a `DROP SCHEMA public CASCADE`
#     being replaced by a schema rebuild only if nothing was dropped, and
#     which no schema.sql anywhere creates, so a database rebuilt from
#     schema.sql cannot have it by any route.
#
# Both are asserted BY CONTENT. A count of one is satisfied by a freshly
# inserted placeholder; the specific name is not.

SENTINEL_TABLE = "w126m_sentinel"
SENTINEL_NOTE = "planted-before-the-upgrade-and-must-survive-it"
SENTINEL_ORG = "Sentinel Organisation w126m"


def plant_sentinel(connection) -> str:
    """Write both markers into a database that ALREADY holds the clinical
    schema. Returns the organisation's id."""
    with connection.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {SENTINEL_TABLE} (note TEXT PRIMARY KEY)"  # noqa: S608 -- constant
        )
        cur.execute(f"INSERT INTO {SENTINEL_TABLE} (note) VALUES (%s)", (SENTINEL_NOTE,))
        cur.execute(
            "INSERT INTO organisations (name, created_at) VALUES (%s, now()) RETURNING org_id",
            (SENTINEL_ORG,),
        )
        org_id = cur.fetchone()[0]
    connection.commit()
    return str(org_id)


def read_sentinel(connection) -> tuple:
    """
    (sentinel notes, organisation names) as they are RIGHT NOW.

    Returns content, never counts: the caller compares against the exact
    strings it planted. A dropped-and-recreated database returns ([], []) --
    or raises UndefinedTable for the sentinel table, which is why that is
    caught and reported as the empty tuple rather than as an error.
    """
    return (
        _rows(connection, f"SELECT note FROM {SENTINEL_TABLE} ORDER BY note"),  # noqa: S608
        _rows(connection, "SELECT name FROM organisations ORDER BY name"),
    )


def _rows(connection, query: str) -> tuple:
    """One statement, its own cursor, its own transaction. A missing table
    aborts the transaction in Postgres, so each read is isolated -- otherwise
    the first absent sentinel would poison the second read and both would
    report empty for one reason."""
    try:
        with connection.cursor() as cur:
            cur.execute(query)
            return tuple(r[0] for r in cur.fetchall())
    except Exception:  # noqa: BLE001 -- an absent table IS the finding, not an error
        return ()
    finally:
        connection.rollback()
