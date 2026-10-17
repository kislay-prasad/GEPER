"""
clinical/tests/test_w165_provision_cli.py
─────────────────────────────────────────
Stage 1 of the backend completion: the operator CLI ``clinical/provision.py``.

The behaviour of ``provision_organisation`` is w153's and is not re-proved here.
This file proves what the CLI adds, and what it must never become:

  * the password cannot be supplied on the command line;
  * weak/blank inputs are refused BEFORE any database work;
  * a non-interactive run needs an explicit ``--yes``;
  * the CLI connects as ``clinical_app`` (least privilege) and produces an
    administrator who can log in, with an audit row that names the OS user and
    host and does not contain the password;
  * a database behind the expected schema version is refused with nothing written;
  * no served route imports the module.

Database rows skip without CLINICAL_TEST_DSN, like the rest of the package.
"""

from __future__ import annotations

import ast
import os
import pathlib
import uuid

import pytest

from clinical import provision

DSN = os.getenv("CLINICAL_TEST_DSN")
APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
GOOD_PASSWORD = "correct-horse-battery"


# ── no database needed ───────────────────────────────────────────────────────


class TestInputsAreRefusedBeforeAnyDatabaseWork:
    @pytest.mark.parametrize("bad", ["", "short", " leading-space-pw", "trailing-space-pw ", "x" * 73])
    def test_weak_passwords_are_refused(self, bad):
        with pytest.raises(provision.ProvisionError):
            provision.validate_password(bad)

    def test_a_good_password_is_accepted(self):
        provision.validate_password(GOOD_PASSWORD)

    @pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "two words@x.org"])
    def test_malformed_emails_are_refused(self, bad):
        with pytest.raises(provision.ProvisionError):
            provision.validate_email(bad)

    def test_blank_operating_context_is_refused(self):
        with pytest.raises(provision.ProvisionError):
            provision.operating_context("   ")

    def test_operating_context_names_the_os_user_and_host(self):
        text = provision.operating_context("onboarding OPS-1")
        assert text.startswith("onboarding OPS-1 [os-user=") and "host=" in text

    def test_provision_validates_before_touching_the_connection(self):
        class Boom:
            def __getattr__(self, name):  # any use of the connection fails the test
                raise AssertionError("connection was touched")

        with pytest.raises(provision.ProvisionError):
            provision.provision(
                Boom(), org_name="H", admin_email="a@b.org", admin_password="short", context="c", data_access=Boom()
            )


class TestThePasswordNeverTravelsOnTheCommandLine:
    def test_there_is_no_password_option(self):
        parser = provision._build_parser()
        flags = {s for action in parser._actions for s in action.option_strings}
        assert not any("password" in f or "secret" in f for f in flags), flags

    def test_passing_one_anyway_is_a_usage_error(self):
        with pytest.raises(SystemExit):
            provision.main(
                ["--org-name", "H", "--admin-email", "a@b.org", "--operating-context", "c", "--password", "x"]
            )


class TestMainRefusals:
    ARGS = ["--org-name", "H", "--admin-email", "a@b.org", "--operating-context", "ctx"]

    def test_no_dsn_exits_1(self, monkeypatch, capsys):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        assert provision.main(self.ARGS) == 1
        assert "CLINICAL_DSN" in capsys.readouterr().err

    def test_non_interactive_without_yes_is_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://unused")
        monkeypatch.setenv("CLINICAL_FIRST_ADMIN_PASSWORD", GOOD_PASSWORD)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert provision.main(self.ARGS) == 2
        assert "--yes" in capsys.readouterr().err

    def test_non_interactive_without_password_env_is_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://unused")
        monkeypatch.delenv("CLINICAL_FIRST_ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert provision.main(self.ARGS + ["--yes"]) == 2

    def test_a_weak_password_is_refused_and_not_echoed(self, monkeypatch, capsys):
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://unused")
        monkeypatch.setenv("CLINICAL_FIRST_ADMIN_PASSWORD", "tooshort")
        assert provision.main(self.ARGS + ["--yes"]) == 2
        captured = capsys.readouterr()
        assert "tooshort" not in captured.out + captured.err


class TestItIsOnNoServedSurface:
    def test_no_application_module_imports_the_cli(self):
        root = pathlib.Path(provision.__file__).parent
        offenders = []
        for path in root.glob("*.py"):
            if path.name == "provision.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = {node.module or ""} | {a.name for a in node.names}
                elif isinstance(node, ast.Import):
                    names = {a.name for a in node.names}
                else:
                    continue
                if any(n == "provision" or n.endswith(".provision") for n in names):
                    offenders.append(path.name)
        assert offenders == []


# ── database rows ────────────────────────────────────────────────────────────


@pytest.fixture()
def app_dsn(monkeypatch):
    """A freshly bootstrapped AND migrated database; returns the clinical_app DSN."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from clinical.bootstrap import apply_schema, ensure_roles

    with psycopg.connect(DSN, autocommit=False, connect_timeout=10) as admin:
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        ensure_roles(admin, app_password=APP_PASSWORD, retention_password=RETENTION_PASSWORD)
        apply_schema(admin)
        admin.commit()

    app = make_conninfo(DSN, user="clinical_app", password=APP_PASSWORD)
    assert conninfo_to_dict(app)["user"] == "clinical_app"
    monkeypatch.setenv("CLINICAL_DSN", app)
    monkeypatch.setenv("CLINICAL_FIRST_ADMIN_PASSWORD", GOOD_PASSWORD)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    return app


def _admin_rows(sql, params=()):
    import psycopg

    with psycopg.connect(DSN, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


ARGS = [
    "--org-name",
    "St Example Hospital",
    "--admin-email",
    "admin@example.test",
    "--operating-context",
    "w165 test run",
    "--yes",
]


class TestTheCliProducesAWorkingAdministrator:
    def test_it_prints_an_org_id_and_the_administrator_can_log_in(self, app_dsn, capsys):
        import psycopg

        from clinical.data_access import DataAccess

        assert provision.main(ARGS) == 0
        out = capsys.readouterr().out
        assert GOOD_PASSWORD not in out
        org_id = uuid.UUID(out.strip().removeprefix("org_id="))

        with psycopg.connect(app_dsn, connect_timeout=10) as conn:
            session = DataAccess(conn).login("admin@example.test", org_id, GOOD_PASSWORD)
            assert [a.role for a in DataAccess(conn).get_user_roles(session, session.user_id)] == ["Administrator"]

    def test_the_audit_row_names_the_operator_and_never_the_password(self, app_dsn, capsys):
        assert provision.main(ARGS) == 0
        rows = _admin_rows(
            "SELECT action, outcome, details::text FROM audit_log WHERE action = 'organisation_provisioned'"
        )
        assert len(rows) == 1
        details = rows[0][2]
        assert "w165 test run [os-user=" in details and "host=" in details
        assert GOOD_PASSWORD not in details

    def test_a_refused_password_writes_nothing(self, app_dsn, monkeypatch, capsys):
        monkeypatch.setenv("CLINICAL_FIRST_ADMIN_PASSWORD", "short")
        assert provision.main(ARGS) == 2
        assert _admin_rows("SELECT count(*) FROM organisations")[0][0] == 0
        assert _admin_rows("SELECT count(*) FROM users")[0][0] == 0

    def test_it_connects_as_the_least_privileged_role(self, app_dsn):
        import psycopg

        with psycopg.connect(app_dsn, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            assert cur.fetchone() == ("clinical_app", False)


class TestAnUnmigratedDatabaseIsRefused:
    def test_nothing_is_written_when_the_schema_version_is_wrong(self, app_dsn, capsys):
        import psycopg

        with psycopg.connect(DSN, connect_timeout=10) as admin:
            admin.execute("DELETE FROM schema_migrations")
            admin.commit()
        assert provision.main(ARGS) == 2
        assert "schema version" in capsys.readouterr().err
        assert _admin_rows("SELECT count(*) FROM organisations")[0][0] == 0
