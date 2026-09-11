"""
clinical/tests/db_guard.py
──────────────────────────
The disposable-database guard. Shared by clinical/tests/conftest.py and
geper/api/tests/conftest.py, the two suites that point fixtures at
CLINICAL_TEST_DSN.

WHY IT EXISTS: every database fixture in these suites begins with
`DROP SCHEMA public CASCADE` against whatever database CLINICAL_TEST_DSN
names. Setting the DSN has been the only thing standing between a test run
and a wiped database, and the one address this floor's documentation
points people at (localhost:5433, the shared `kelly-clinical-test`
container) holds other people's work. Pointing the DSN there by habit or by
a stale shell variable used to erase it silently.

So a DSN alone no longer runs anything. The run must ALSO say, in a second
variable, that the database it names is disposable:

    CLINICAL_TEST_DB_DISPOSABLE=1

CI sets it next to its own throwaway service container. A local run sets it
next to a container the developer started for this purpose (see the refusal
message below). Without it the session stops before any connection is
opened -- before the reachability probe, before any fixture.

Deliberately NOT a hostname or port blocklist: a blocklist protects only
the addresses someone remembered to list, and the next shared database
will not be on it. The opt-in is a statement about the database, made by
the person who knows which database it is.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DSN_ENV = "CLINICAL_TEST_DSN"
DISPOSABLE_ENV = "CLINICAL_TEST_DB_DISPOSABLE"
REQUIRE_DB_ENV = "CLINICAL_REQUIRE_DB"

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

_CREATE_ROLES = (
    "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
)


def is_disposable() -> bool:
    """Exactly "1". Not truthiness: "true", "yes" or a stray space are not a
    deliberate statement that a database may be erased."""
    return os.getenv(DISPOSABLE_ENV, "") == "1"


def require_db() -> bool:
    """Same reading as clinical/tests/conftest.py::_require_db."""
    return os.getenv(REQUIRE_DB_ENV, "").strip() not in ("", "0", "false", "False")


def _address(dsn: str) -> str:
    """host/port/dbname only -- the DSN carries a password and this string
    goes into CI logs (same rule as conftest.py::_address_only)."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(dsn)
        return (
            f"host={parsed.get('host') or '(default)'} "
            f"port={parsed.get('port') or 5432} "
            f"dbname={parsed.get('dbname') or '(default)'}"
        )
    except Exception:
        return "(unparseable DSN)"


def refusal_message(dsn: str) -> str:
    return "\n".join(
        [
            "",
            f"{DSN_ENV} is set ({_address(dsn)}), but {DISPOSABLE_ENV}=1 is not.",
            "",
            "These tests run `DROP SCHEMA public CASCADE` on that database before every",
            "test. Refusing to touch it until you state that it is disposable.",
            "",
            "Run against a database that exists only for this run, e.g.:",
            "    docker run --rm -d --name <you>-pg -e POSTGRES_PASSWORD=<pw> -p <free port>:5432 postgres:16",
            f"    {DSN_ENV}=postgresql://postgres:<pw>@127.0.0.1:<free port>/postgres",
            f"    {DISPOSABLE_ENV}=1",
            "",
            "Never point it at a shared container (such as kelly-clinical-test on 5433):",
            "the suite would erase it. To run only the offline tests, unset the DSN.",
            "",
        ]
    )


def exit_unless_disposable(dsn: str) -> None:
    """Stop the whole session (exit code 4, the same code the reachability
    gate uses) unless the DSN has been declared disposable."""
    if not is_disposable():
        pytest.exit(refusal_message(dsn), returncode=4)


def reset_schema(connection) -> None:
    """The fixture body every clinical test file already inlines: wipe, recreate
    the two roles the schema grants to, load schema.sql, commit. Only ever
    called after exit_unless_disposable has passed."""
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for statement in _CREATE_ROLES:
            cur.execute(statement)
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
