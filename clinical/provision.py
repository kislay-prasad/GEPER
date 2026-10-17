"""
clinical/provision.py
─────────────────────
The operator CLI for hospital onboarding: one organisation and its first
Administrator, created by ``DataAccess.provision_organisation`` and nothing else.

THIS FILE IS A WRAPPER, NOT A MECHANISM. The w128 ruling and the w153 tests
own the behaviour: organisation, first user and the 'Administrator' role in ONE
transaction, one ``organisation_provisioned`` audit row (or its ``_failed`` /
``_denied`` twin), the password hashed with bcrypt and absent from the audit
details. This module adds only what a command line needs: a way to supply a
password that is not ``argv``, a required statement of who ran it and why, a
refusal to run against a database that is not at the expected schema version,
and a confirmation step.

IT IS ON NO SERVED SURFACE. No route imports it and none may
(``test_w153_first_administrator_bootstrap`` scans every route module, and
``test_w165_provision_cli`` scans for an import of this module). There is no
HTTP registration of administrators.

USAGE::

    export CLINICAL_DSN=postgresql://clinical_app:...@host:5432/clinical
    export CLINICAL_FIRST_ADMIN_PASSWORD=...        # or omit to be prompted
    python -m clinical.provision \\
        --org-name "St Example Hospital" \\
        --admin-email admin@example.org \\
        --operating-context "Initial onboarding, ticket OPS-123"

It connects as ``clinical_app`` (``CLINICAL_DSN``), the least-privileged role
that already holds INSERT on the three tables, and not as the administrative
role. On success it prints the new ``org_id`` -- the value ``login`` needs --
and nothing secret. A second run creates a SECOND organisation with its own
administrator (spec 4.2 multi-organisation tenancy); it cannot address an
existing one, so it is not a way to add an administrator to an organisation
that already has one.

THE ACTOR IS NOT A DATABASE PRINCIPAL. The audit row carries no user, so the
operating context is the only accountability; this CLI appends the operating
system user and host to it so the row names who ran the command.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import socket
import sys
from typing import List, Optional

_DSN_ENV = "CLINICAL_DSN"
_PASSWORD_ENV = "CLINICAL_FIRST_ADMIN_PASSWORD"

MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProvisionError(Exception):
    """A refusal with a message that is safe to print (never contains a secret)."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ProvisionError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ProvisionError(f"password must be at most {_MAX_PASSWORD_BYTES} bytes (bcrypt limit)")
    if password.strip() != password:
        raise ProvisionError("password must not begin or end with whitespace")


def validate_email(email: str) -> str:
    cleaned = email.strip()
    if not _EMAIL.match(cleaned):
        raise ProvisionError("admin email is not a valid address")
    return cleaned


def read_password(*, interactive: bool) -> str:
    """Environment first, then a no-echo prompt (twice). Never ``argv``."""
    supplied = os.environ.get(_PASSWORD_ENV)
    if supplied:
        return supplied
    if not interactive:
        raise ProvisionError(f"{_PASSWORD_ENV} is not set and there is no terminal to prompt on")
    first = getpass.getpass("First administrator password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise ProvisionError("passwords do not match")
    return first


def operating_context(statement: str) -> str:
    statement = statement.strip()
    if not statement:
        raise ProvisionError("--operating-context must say who is doing this and why")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 -- no user database entry is not a reason to refuse
        user = "unknown"
    return f"{statement} [os-user={user} host={socket.gethostname()}]"


def provision(
    connection,
    *,
    org_name: str,
    admin_email: str,
    admin_password: str,
    context: str,
    full_name: Optional[str] = None,
    registration_number: Optional[str] = None,
    hospital: Optional[str] = None,
    data_access=None,
):
    """Validate, then call the one audited domain method. Returns the org_id."""
    if not org_name.strip():
        raise ProvisionError("--org-name must not be blank")
    email = validate_email(admin_email)
    validate_password(admin_password)

    if data_access is None:
        from clinical.data_access import DataAccess

        data_access = DataAccess(connection)
    return data_access.provision_organisation(
        name=org_name.strip(),
        admin_email=email,
        admin_password=admin_password,
        operating_context=context,
        full_name=full_name,
        registration_number=registration_number,
        hospital=hospital,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m clinical.provision",
        description="Create a hospital organisation and its first Administrator (operator-run, audited).",
    )
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--operating-context", required=True, help="who is running this and why (audited)")
    parser.add_argument("--full-name")
    parser.add_argument("--registration-number")
    parser.add_argument("--hospital")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    interactive = sys.stdin.isatty()

    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        print(f"clinical.provision: {_DSN_ENV} is not set.", file=sys.stderr)
        return 1

    try:
        context = operating_context(args.operating_context)
        email = validate_email(args.admin_email)
        password = read_password(interactive=interactive)
        validate_password(password)

        if not args.yes:
            if not interactive:
                raise ProvisionError("not a terminal: pass --yes to confirm non-interactively")
            answer = input(f"Create organisation {args.org_name!r} with administrator {email}? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                print("clinical.provision: cancelled.", file=sys.stderr)
                return 1

        import psycopg

        from clinical import migrate

        with psycopg.connect(dsn, autocommit=False, connect_timeout=10) as connection:
            state = migrate.inspect_state(connection)
            connection.rollback()
            if not state.ok:
                raise ProvisionError(
                    f"database is not at the expected schema version ({state.reason}); run clinical.migrate"
                )
            org_id = provision(
                connection,
                org_name=args.org_name,
                admin_email=email,
                admin_password=password,
                context=context,
                full_name=args.full_name,
                registration_number=args.registration_number,
                hospital=args.hospital,
            )
    except ProvisionError as exc:
        print(f"clinical.provision: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- report the type only; a message may echo a value
        print(
            f"clinical.provision: failed ({type(exc).__name__}); see the audit log for the _failed row.",
            file=sys.stderr,
        )
        return 3

    print(f"org_id={org_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
