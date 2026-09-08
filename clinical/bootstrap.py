"""
clinical/bootstrap.py
─────────────────────
Creates the two Postgres login roles that ``clinical/schema.sql`` grants to,
so the schema can be applied to an empty database.

WHY THIS FILE EXISTS AT ALL. ``schema.sql`` carried two comments --
``-- CREATE ROLE clinical_app LOGIN PASSWORD '...';   -- deployment, not here``
and the same for ``clinical_retention`` -- while every GRANT and REVOKE naming
those roles was a real statement. On a database where neither role existed,
applying the schema failed at the first ``GRANT USAGE ON SCHEMA public TO
clinical_app``. The handoff named a "there" that did not exist. This file is
that "there".

WHY THE TEST SUITE DID NOT CATCH IT, which is the part worth remembering: the
``conn`` fixture in ``tests/test_clinical_domain.py`` creates both roles before
applying the schema, so the schema was only ever exercised against a database
that had been prepared by hand. It creates them with neither LOGIN nor a
password, so even the green path never produced a role a service could connect
as. The grants and the refusals verified against those roles are real -- the
privilege model is not in question here -- but the deployment path had never
been run. ``tests/test_bootstrap_empty_database.py`` is the test that would
have caught it, and it deliberately does not use that fixture.

PASSWORDS COME FROM THE OPERATOR, NOT FROM THIS REPOSITORY. There is no secret
store here and this file does not invent one: the two passwords are read from
the environment and have NO DEFAULTS. A missing one is a hard refusal, never a
fallback -- a bootstrap that quietly picks its own password would produce a
database whose credentials nobody knows, which is worse than one that refuses
to be created. How the operator supplies them (a secrets manager, a CI secret,
an interactive shell) is a deployment decision this file deliberately does not
make.

IDEMPOTENT, and deliberately so rather than single-use: a role that already
exists is ALTERed to the supplied password with LOGIN set, not skipped. That
matters beyond re-runs -- it is also what converges a role created the old way
(NOLOGIN, no password, as the test fixture makes them) into one a service can
actually connect as.

RUN IT, against an empty database, as a superuser or a role with CREATEROLE:

    export CLINICAL_BOOTSTRAP_DSN=postgresql://postgres:...@host:5432/clinical
    export CLINICAL_APP_PASSWORD=...
    export CLINICAL_RETENTION_PASSWORD=...
    python -m clinical.bootstrap --apply-schema

``--apply-schema`` then applies ``schema.sql`` in the same connection and the
same transaction, so an empty database reaches a usable state in one command
and a failure leaves nothing half-applied. Without the flag it creates the
roles only, for a deployment that applies the schema by some other route
(``psql -f``, a migration tool). The DSN it connects with is the ADMINISTRATIVE
one; it is not ``CLINICAL_DSN``, which is what the application later connects
with as ``clinical_app``.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Dict

# The role names are fixed identifiers that must match schema.sql's GRANT
# targets exactly. They are not configurable: a deployment that renamed them
# would have to edit the grants too, and then the schema and the bootstrap
# would disagree silently.
APP_ROLE = "clinical_app"
RETENTION_ROLE = "clinical_retention"

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "schema.sql"

_APP_PASSWORD_ENV = "CLINICAL_APP_PASSWORD"
_RETENTION_PASSWORD_ENV = "CLINICAL_RETENTION_PASSWORD"
_BOOTSTRAP_DSN_ENV = "CLINICAL_BOOTSTRAP_DSN"


def ensure_roles(connection, *, app_password: str, retention_password: str) -> Dict[str, str]:
    """
    Create or update the two login roles ``schema.sql`` grants to.

    Returns a dict mapping role name to ``"created"`` or ``"updated"``, so a
    caller can report what actually happened rather than assuming. Does not
    commit -- the caller owns the transaction, which is what lets
    ``--apply-schema`` put role creation and schema application in one.

    An empty password is refused rather than passed through: Postgres accepts
    ``PASSWORD ''`` and produces a role that cannot authenticate, which is the
    same silent-nothing this file exists to end.
    """
    from psycopg import sql

    if not app_password:
        raise ValueError(f"{APP_ROLE} password is empty; refusing to create a role that cannot authenticate")
    if not retention_password:
        raise ValueError(f"{RETENTION_ROLE} password is empty; refusing to create a role that cannot authenticate")

    result: Dict[str, str] = {}
    for role, password in ((APP_ROLE, app_password), (RETENTION_ROLE, retention_password)):
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            exists = cur.fetchone() is not None
            # CREATE/ALTER ROLE are utility statements: Postgres does not
            # accept bound parameters in them, so the password is composed as
            # a literal by psycopg.sql, which quotes it correctly. Passing it
            # through %s would fail at the server, and building the string by
            # hand would be an injection.
            if exists:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
                )
                result[role] = "updated"
            else:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
                )
                result[role] = "created"
    return result


def apply_schema(connection) -> None:
    """Apply ``schema.sql`` on an open connection. Does not commit."""
    with connection.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"clinical.bootstrap: {name} is not set. It has no default on purpose -- "
            "see this module's docstring on why a bootstrap must refuse rather than "
            "invent a credential.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m clinical.bootstrap",
        description="Create the clinical_app and clinical_retention login roles that clinical/schema.sql grants to.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="also apply clinical/schema.sql, in the same transaction as the role creation",
    )
    args = parser.parse_args(argv)

    dsn = _require_env(_BOOTSTRAP_DSN_ENV)
    app_password = _require_env(_APP_PASSWORD_ENV)
    retention_password = _require_env(_RETENTION_PASSWORD_ENV)

    import psycopg

    with psycopg.connect(dsn, autocommit=False) as connection:
        outcome = ensure_roles(
            connection,
            app_password=app_password,
            retention_password=retention_password,
        )
        if args.apply_schema:
            apply_schema(connection)
        connection.commit()

    for role, what in sorted(outcome.items()):
        print(f"{role}: {what} (LOGIN, password set)")
    print(f"schema.sql: {'applied' if args.apply_schema else 'not applied (--apply-schema not given)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
