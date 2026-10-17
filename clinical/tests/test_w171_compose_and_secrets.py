"""
clinical/tests/test_w171_compose_and_secrets.py
───────────────────────────────────────────────
Stage 4, increment 2: the separate clinical Compose project
(`docker-compose.clinical.yml`), the secrets script (`scripts/clinical-secrets.sh`)
and the deployment document that both point at.

NO DOCKER AND NO DATABASE IS NEEDED. The Compose file is parsed, not run, and the
script is run under `sh` in a temporary directory. What this cannot show -- that
the stack starts, that init is idempotent, that login works -- was shown by
running it (docs/CLINICAL_DEPLOYMENT.md, "Verification"), and not by this file.

TWO ROWS ARE CONDITIONAL, AND SAY SO. Permission-mode assertions are made only
where the filesystem reports POSIX modes (a Windows checkout does not), and the
git-shim rows need a POSIX shell with executable files. Both always run in CI and
in the container `test` target, which are Linux.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess

import pytest
import yaml

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
REPO = CLINICAL.parent
SCRIPT = REPO / "scripts" / "clinical-secrets.sh"
COMPOSE = REPO / "docker-compose.clinical.yml"
ENV_EXAMPLE = REPO / ".env.clinical.example"
DOC = REPO / "docs" / "CLINICAL_DEPLOYMENT.md"

SECRET_FILES = (
    "postgres_password",
    "clinical_app_password",
    "clinical_retention_password",
    "clinical_first_admin_password",
    "clinical_totp_secret_keys",
    "clinical_dsn",
    "clinical_bootstrap_dsn",
)

POSIX = os.name == "posix"
requires_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on this machine")
requires_posix = pytest.mark.skipif(not POSIX, reason="needs executable shim files (always runs on Linux)")


# ═════════════════════════════════════════════════════════════════════════════
# THE SECRETS SCRIPT
# ═════════════════════════════════════════════════════════════════════════════


def _run(args, cwd, env_extra=None, path_prepend=None):
    env = dict(os.environ)
    env.pop("CLINICAL_SECRETS_DIR", None)
    if env_extra:
        env.update(env_extra)
    if path_prepend:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["sh", str(SCRIPT).replace("\\", "/"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=60,
    )


def _dir(tmp_path, name="sec"):
    return str(tmp_path / name).replace("\\", "/")


def _read(directory, name):
    return (pathlib.Path(directory) / name).read_text(encoding="utf-8")


def _modes_enforced(tmp_path):
    probe = tmp_path / "probe"
    probe.write_text("x")
    os.chmod(probe, 0o640)
    return stat.S_IMODE(probe.stat().st_mode) == 0o640


@requires_sh
class TestTheScriptCreatesWhatIsMissingAndNothingElse:
    def test_it_creates_all_seven_files_with_the_right_shapes(self, tmp_path):
        d = _dir(tmp_path)
        result = _run(["--dir", d], tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert sorted(p.name for p in pathlib.Path(d).iterdir()) == sorted(SECRET_FILES)
        for name in ("postgres_password", "clinical_app_password", "clinical_retention_password"):
            assert re.fullmatch(r"[0-9a-f]{48}\n", _read(d, name)), name
        key = _read(d, "clinical_totp_secret_keys").strip()
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}=", key), "not Fernet-shaped"
        try:  # the real test of a Fernet key, where the package is installed
            from cryptography.fernet import Fernet

            Fernet(key.encode())
        except ImportError:
            pass
        assert _read(d, "clinical_dsn").strip() == (
            f"postgresql://clinical_app:{_read(d, 'clinical_app_password').strip()}@clinical-db:5432/clinical"
        )
        assert _read(d, "clinical_bootstrap_dsn").strip() == (
            f"postgresql://postgres:{_read(d, 'postgres_password').strip()}@clinical-db:5432/clinical"
        )

    def test_the_host_and_database_flags_reach_both_connection_strings(self, tmp_path):
        d = _dir(tmp_path)
        assert _run(["--dir", d, "--db-host", "db.internal", "--db-name", "ehr"], tmp_path).returncode == 0
        for name in ("clinical_dsn", "clinical_bootstrap_dsn"):
            assert _read(d, name).strip().endswith("@db.internal:5432/ehr"), name

    def test_two_runs_in_different_directories_never_share_a_value(self, tmp_path):
        a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
        _run(["--dir", a], tmp_path)
        _run(["--dir", b], tmp_path)
        for name in SECRET_FILES:
            assert _read(a, name) != _read(b, name), name

    @pytest.mark.parametrize(
        "flag", [["--db-host", "bad host"], ["--db-name", "x;y"], ["--db-host", "a/b"], ["--db-name", ""]]
    )
    def test_a_database_name_that_could_alter_a_connection_string_is_refused_before_anything_is_written(
        self, tmp_path, flag
    ):
        d = _dir(tmp_path)
        result = _run(["--dir", d, *flag], tmp_path)
        assert result.returncode == 2
        assert not pathlib.Path(d).exists()

    def test_an_unknown_argument_is_a_usage_error(self, tmp_path):
        result = _run(["--force"], tmp_path)
        assert result.returncode == 2 and "unknown argument" in result.stderr


@requires_sh
class TestTheScriptNeverOverwritesAndNeverPrints:
    def test_a_second_run_changes_nothing_and_says_kept(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        before = {n: _read(d, n) for n in SECRET_FILES}
        result = _run(["--dir", d], tmp_path)
        assert result.returncode == 0
        assert {n: _read(d, n) for n in SECRET_FILES} == before
        assert result.stdout.count("kept") == 7 and "created" not in result.stdout.replace("created  directory", "")

    def test_an_existing_secret_is_preserved_byte_for_byte_and_the_derived_dsn_follows_it(self, tmp_path):
        d = _dir(tmp_path)
        pathlib.Path(d).mkdir()
        os.chmod(d, 0o700)
        (pathlib.Path(d) / "clinical_app_password").write_text("my-own-chosen-password\n")
        os.chmod(pathlib.Path(d) / "clinical_app_password", 0o600)
        result = _run(["--dir", d], tmp_path)
        assert result.returncode == 0, result.stdout
        assert _read(d, "clinical_app_password") == "my-own-chosen-password\n"
        assert ":my-own-chosen-password@" in _read(d, "clinical_dsn")
        assert "kept     clinical_app_password" in result.stdout

    def test_a_pre_existing_file_with_loose_permissions_is_kept_untouched_but_flagged(self, tmp_path):
        """The script does not silently "fix" a file it did not create (that would be
        modifying an existing secret), and does not pass it either."""
        d = _dir(tmp_path)
        pathlib.Path(d).mkdir()
        os.chmod(d, 0o700)
        loose = pathlib.Path(d) / "clinical_app_password"
        loose.write_text("loosely-held-password\n")
        os.chmod(loose, 0o644)
        if not (POSIX and _modes_enforced(tmp_path)):
            return  # no POSIX modes here; the Linux runs assert it
        result = _run(["--dir", d], tmp_path)
        assert result.returncode == 1
        assert "clinical_app_password mode is 644" in result.stdout
        assert loose.read_text() == "loosely-held-password\n"
        assert stat.S_IMODE(loose.stat().st_mode) == 0o644  # not changed behind the operator's back

    def test_a_deleted_file_is_recreated_and_its_neighbours_are_untouched(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        before = {n: _read(d, n) for n in SECRET_FILES}
        (pathlib.Path(d) / "clinical_first_admin_password").unlink()
        _run(["--dir", d], tmp_path)
        after = {n: _read(d, n) for n in SECRET_FILES}
        assert after["clinical_first_admin_password"] != before["clinical_first_admin_password"]
        assert {k: v for k, v in after.items() if k != "clinical_first_admin_password"} == {
            k: v for k, v in before.items() if k != "clinical_first_admin_password"
        }

    def test_a_symlink_in_the_way_is_not_followed_or_replaced(self, tmp_path):
        d = pathlib.Path(_dir(tmp_path))
        d.mkdir()
        target = tmp_path / "elsewhere"
        target.write_text("do-not-touch\n")
        try:
            (d / "postgres_password").symlink_to(target)
        except OSError:  # symlinks unavailable (Windows without privilege); Linux always runs this
            return
        _run(["--dir", str(d).replace("\\", "/")], tmp_path)
        assert target.read_text() == "do-not-touch\n"
        assert (d / "postgres_password").is_symlink()

    def test_no_secret_value_appears_in_any_output(self, tmp_path):
        d = _dir(tmp_path)
        outputs = [_run(["--dir", d], tmp_path), _run(["--dir", d], tmp_path), _run(["--check", "--dir", d], tmp_path)]
        text = "".join(o.stdout + o.stderr for o in outputs)
        for name in SECRET_FILES:
            value = _read(d, name).strip()
            assert value not in text, f"{name} was printed"
            # nor any 16-character slice of it, which would still be most of a password
            assert value[:16] not in text, f"{name} was partly printed"


@requires_sh
class TestTheScriptCreatesNothingReadableByOthers:
    def test_directory_700_and_files_600_where_the_filesystem_can_say(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        if not _modes_enforced(tmp_path):
            return  # a Windows checkout reports no POSIX modes; the Linux runs below assert it
        assert stat.S_IMODE(pathlib.Path(d).stat().st_mode) == 0o700
        for name in SECRET_FILES:
            assert stat.S_IMODE((pathlib.Path(d) / name).stat().st_mode) == 0o600, name

    def test_a_restrictive_umask_is_not_widened_and_a_permissive_one_is_not_inherited(self, tmp_path):
        if not POSIX:
            return
        d = _dir(tmp_path)
        script = f"umask 000; sh {str(SCRIPT).replace(chr(92), '/')} --dir {d} >/dev/null"
        subprocess.run(["sh", "-c", script], check=True, timeout=60, cwd=tmp_path)
        assert stat.S_IMODE(pathlib.Path(d).stat().st_mode) == 0o700
        for name in SECRET_FILES:
            assert stat.S_IMODE((pathlib.Path(d) / name).stat().st_mode) == 0o600, name


@requires_sh
class TestCheckMode:
    def test_a_fresh_directory_passes_and_nothing_is_changed_by_checking(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        snapshot = {n: (pathlib.Path(d) / n).read_bytes() for n in SECRET_FILES}
        result = _run(["--check", "--dir", d], tmp_path)
        assert result.returncode == 0, result.stdout
        assert "no problems found" in result.stdout
        assert {n: (pathlib.Path(d) / n).read_bytes() for n in SECRET_FILES} == snapshot

    def test_a_missing_directory_and_missing_files_are_failures(self, tmp_path):
        assert _run(["--check", "--dir", _dir(tmp_path, "nope")], tmp_path).returncode == 1
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        (pathlib.Path(d) / "clinical_dsn").unlink()
        result = _run(["--check", "--dir", d], tmp_path)
        assert result.returncode == 1 and "clinical_dsn is missing" in result.stdout

    def test_empty_and_multiline_files_are_failures(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        (pathlib.Path(d) / "postgres_password").write_text("")
        (pathlib.Path(d) / "clinical_first_admin_password").write_text("one\ntwo\n")
        result = _run(["--check", "--dir", d], tmp_path)
        assert result.returncode == 1
        assert "postgres_password is empty" in result.stdout and "more than one line" in result.stdout

    def test_a_dsn_that_no_longer_matches_its_password_is_reported_as_a_note_not_a_leak(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        (pathlib.Path(d) / "clinical_app_password").write_text("a-different-password\n")
        result = _run(["--check", "--dir", d], tmp_path)
        assert "does not contain clinical_app_password" in result.stdout
        assert "a-different-password" not in result.stdout

    def test_a_malformed_dsn_or_key_is_a_failure(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        (pathlib.Path(d) / "clinical_dsn").write_text("mysql://u:p@h/db\n")
        (pathlib.Path(d) / "clinical_totp_secret_keys").write_text("not-a-fernet-key\n")
        result = _run(["--check", "--dir", d], tmp_path)
        assert result.returncode == 1
        assert "not of the form postgresql://" in result.stdout and "not a list of Fernet keys" in result.stdout

    def test_a_rotation_list_of_two_keys_is_accepted(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        old = _read(d, "clinical_totp_secret_keys").strip()
        new = old[:42] + ("A" if old[42] != "A" else "B") + "="  # a different, still well-formed Fernet-shaped key
        (pathlib.Path(d) / "clinical_totp_secret_keys").write_text(f"{new},{old}\n")
        assert _run(["--check", "--dir", d], tmp_path).returncode == 0

    def test_loose_modes_are_failures_where_the_filesystem_can_say(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        if not (POSIX and _modes_enforced(tmp_path)):
            return
        os.chmod(pathlib.Path(d) / "clinical_dsn", 0o644)
        os.chmod(d, 0o755)
        result = _run(["--check", "--dir", d], tmp_path)
        assert result.returncode == 1
        assert "clinical_dsn mode is 644" in result.stdout and "directory mode is 755" in result.stdout


@requires_sh
@requires_posix
class TestTheScriptWillNotWriteWhereGitWouldTrackIt:
    """`git` is replaced by a shim on PATH, so each of git's answers is exercised
    without depending on this repository's own state."""

    def _shim(self, tmp_path, ls_files="", ignore_rc=0):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            f'case "$1" in\n  ls-files) printf "%s" "{ls_files}" ;;\n  check-ignore) exit {ignore_rc} ;;\n  *) exit 0 ;;\nesac\n'
        )
        shim.chmod(0o755)
        return str(bin_dir)

    def test_not_ignored_means_refuse_and_create_nothing(self, tmp_path):
        d = _dir(tmp_path)
        result = _run(["--dir", d], tmp_path, path_prepend=self._shim(tmp_path, ignore_rc=1))
        assert result.returncode == 1 and "not ignored by git" in result.stderr
        assert not pathlib.Path(d).exists()

    def test_tracked_files_mean_refuse_and_create_nothing(self, tmp_path):
        d = _dir(tmp_path)
        result = _run(["--dir", d], tmp_path, path_prepend=self._shim(tmp_path, ls_files="sec/x\n"))
        assert result.returncode == 1 and "tracks files" in result.stderr
        assert not pathlib.Path(d).exists()

    def test_ignored_proceeds(self, tmp_path):
        d = _dir(tmp_path)
        result = _run(["--dir", d], tmp_path, path_prepend=self._shim(tmp_path, ignore_rc=0))
        assert result.returncode == 0 and "is ignored" in result.stdout

    def test_outside_the_work_tree_proceeds_with_a_note(self, tmp_path):
        d = _dir(tmp_path)
        result = _run(["--dir", d], tmp_path, path_prepend=self._shim(tmp_path, ignore_rc=128))
        assert result.returncode == 0 and "outside this git work tree" in result.stdout

    def test_check_mode_reports_a_tracked_directory_as_a_failure(self, tmp_path):
        d = _dir(tmp_path)
        _run(["--dir", d], tmp_path)
        result = _run(["--check", "--dir", d], tmp_path, path_prepend=self._shim(tmp_path, ls_files="sec/x\n"))
        assert result.returncode == 1 and "TRACKS" in result.stdout


# ═════════════════════════════════════════════════════════════════════════════
# THE COMPOSE FILE
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service):
    env = service.get("environment", {})
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


CLINICAL_SERVICES = ("clinical-init", "clinical-api", "clinical-provision", "clinical-test")


class TestComposeIsolation:
    def test_it_is_its_own_project_and_adds_nothing_to_the_pipeline_compose_file(self, compose):
        assert compose["name"] == "geper-clinical"
        pipeline = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
        assert set(pipeline["services"]) == {"geper"}
        assert set(pipeline["volumes"]) == {"geper_model_cache", "geper_hf_cache"}
        assert not set(compose.get("volumes", {})) & set(pipeline["volumes"])

    def test_no_service_hard_codes_a_container_name(self, compose):
        for name, service in compose["services"].items():
            assert "container_name" not in service, name

    def test_the_default_services_are_only_init_and_the_api(self, compose):
        defaults = {n for n, s in compose["services"].items() if "profiles" not in s}
        assert defaults == {"clinical-init", "clinical-api"}

    def test_the_profiles_are_the_documented_ones(self, compose):
        profiles = {n: tuple(s.get("profiles", ())) for n, s in compose["services"].items()}
        assert profiles["clinical-db"] == ("with-db",)
        assert profiles["clinical-provision"] == ("ops",)
        assert profiles["clinical-test"] == profiles["clinical-test-db"] == ("test",)

    def test_the_only_named_volume_is_the_database_and_nothing_is_external(self, compose):
        assert set(compose["volumes"]) == {"clinical-pgdata"}
        assert not (compose["volumes"]["clinical-pgdata"] or {}).get("external")
        for network in compose["networks"].values():
            assert not (network or {}).get("external")


class TestComposeNetworkExposure:
    def test_only_the_api_publishes_a_port_and_only_on_loopback(self, compose):
        for name, service in compose["services"].items():
            ports = service.get("ports", [])
            if name != "clinical-api":
                assert not ports, f"{name} publishes {ports}"
        (port,) = compose["services"]["clinical-api"]["ports"]
        assert port.startswith("127.0.0.1:") and port.endswith(":8000"), port

    def test_the_database_networks_are_internal(self, compose):
        assert compose["networks"]["db"]["internal"] is True
        assert compose["networks"]["test"]["internal"] is True

    def test_the_databases_sit_only_on_their_own_internal_network(self, compose):
        assert compose["services"]["clinical-db"]["networks"] == ["db"]
        assert compose["services"]["clinical-test-db"]["networks"] == ["test"]
        assert compose["services"]["clinical-test"]["networks"] == ["test"]

    def test_the_api_is_reachable_from_the_app_network_the_database_is_not(self, compose):
        assert set(compose["services"]["clinical-api"]["networks"]) == {"db", "app"}
        assert "app" not in compose["services"]["clinical-db"]["networks"]


class TestComposeNeverTrustsEveryone:
    def test_no_wildcard_trust_anywhere_in_the_file_or_the_example(self):
        for path in (COMPOSE, ENV_EXAMPLE):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"FORWARDED_ALLOW_IPS\s*[:=]\s*['\"]?\*", text), path.name
            assert not re.search(r"forwarded-allow-ips[ =]+['\"]?\*", text, re.I), path.name
            assert not re.search(r"CLINICAL_FORWARDED_ALLOW_IPS\s*=\s*['\"]?\*", text), path.name

    def test_the_default_trusts_only_loopback_which_in_a_container_is_nobody(self, compose):
        value = _env(compose["services"]["clinical-api"])["FORWARDED_ALLOW_IPS"]
        assert value == "${CLINICAL_FORWARDED_ALLOW_IPS:-127.0.0.1}", value

    def test_the_api_has_no_tls_arguments_and_publishes_no_docs(self, compose):
        text = COMPOSE.read_text(encoding="utf-8")
        for tls in ("--ssl-certfile", "--ssl-keyfile", "--ssl-ca-certs"):
            assert tls not in text
        assert "CLINICAL_ENABLE_DOCS" not in _env(compose["services"]["clinical-api"])


class TestComposeSecrets:
    def test_every_secret_is_a_file_under_the_secrets_directory_and_matches_the_script(self, compose):
        declared = compose["secrets"]
        assert set(declared) == set(SECRET_FILES)
        for name, spec in declared.items():
            assert spec == {"file": f"${{CLINICAL_SECRETS_DIR:-./secrets}}/{name}"}, name
        script = SCRIPT.read_text(encoding="utf-8")
        listed = set(re.findall(r"^PASSWORD_FILES=\"([^\"]+)\"", script, re.M)[0].split()) | {
            "clinical_totp_secret_keys",
            "clinical_dsn",
            "clinical_bootstrap_dsn",
        }
        assert listed == set(SECRET_FILES)

    def test_no_service_carries_a_secret_value_in_its_environment(self, compose):
        sensitive = re.compile(r"PASSWORD|DSN|SECRET|KEY|TOKEN|CREDENTIAL", re.I)
        for name, service in compose["services"].items():
            for key, value in _env(service).items():
                if not sensitive.search(key):
                    continue
                if key == "CLINICAL_TEST_DSN":
                    # the disposable test database has no password at all; prove the
                    # string carries none rather than exempting the variable
                    assert name == "clinical-test"
                    assert re.fullmatch(r"postgresql://postgres@clinical-test-db:5432/postgres", value), value
                    continue
                assert key.endswith("_FILE"), f"{name}: {key} is not a *_FILE variable"
                assert value.startswith("/run/secrets/"), f"{name}: {key}={value}"

    def test_each_file_variable_points_at_a_secret_the_service_actually_mounts(self, compose):
        for name, service in compose["services"].items():
            mounted = set(service.get("secrets", []))
            for key, value in _env(service).items():
                if key.endswith("_FILE"):
                    assert value.removeprefix("/run/secrets/") in mounted, f"{name}: {key}"

    def test_the_entrypoint_is_told_about_every_variable_the_stack_loads_from_a_file(self, compose):
        entrypoint = (CLINICAL / "docker-entrypoint.sh").read_text(encoding="utf-8")
        known = set(re.search(r'SECRET_VARIABLES="\n(.*?)"', entrypoint, re.S).group(1).split())
        for name, service in compose["services"].items():
            for key in _env(service):
                if (
                    key.endswith("_FILE") and key != "POSTGRES_PASSWORD_FILE"
                ):  # the postgres image reads that one itself
                    assert key.removesuffix("_FILE") in known, f"{name}: {key} is not read by the entrypoint"

    def test_the_api_does_not_receive_the_database_administrator_or_bootstrap_secrets(self, compose):
        api = set(compose["services"]["clinical-api"]["secrets"])
        assert api == {"clinical_dsn", "clinical_totp_secret_keys"}
        assert set(compose["services"]["clinical-init"]["secrets"]) == {
            "clinical_bootstrap_dsn",
            "clinical_app_password",
            "clinical_retention_password",
        }
        assert set(compose["services"]["clinical-provision"]["secrets"]) == {
            "clinical_dsn",
            "clinical_first_admin_password",
        }

    def test_the_test_database_has_no_password_because_it_is_unreachable_and_disposable(self, compose):
        test_db = compose["services"]["clinical-test-db"]
        assert _env(test_db) == {"POSTGRES_HOST_AUTH_METHOD": "trust"}
        assert test_db["tmpfs"] == ["/var/lib/postgresql/data"] and not test_db.get("volumes")
        assert compose["networks"]["test"]["internal"] is True

    def test_the_disposable_database_flags_are_set_for_the_test_service_and_nowhere_else(self, compose):
        for name, service in compose["services"].items():
            env = _env(service)
            has = "CLINICAL_TEST_DB_DISPOSABLE" in env or "CLINICAL_REQUIRE_DB" in env
            assert has == (name == "clinical-test"), name


class TestComposeHardeningAndOrdering:
    @pytest.mark.parametrize("name", CLINICAL_SERVICES)
    def test_clinical_containers_are_unprivileged_read_only_and_capability_free(self, compose, name):
        service = compose["services"][name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert re.fullmatch(r"\$\{CLINICAL_UID:-10001\}:\$\{CLINICAL_GID:-10001\}", service["user"])
        # /tmp is a tmpfs, and is `noexec` (Docker's default) everywhere EXCEPT the
        # disposable test runner, whose tests create and run small helper scripts.
        expected = "/tmp:exec" if name == "clinical-test" else "/tmp"
        assert service["tmpfs"] == [expected], service["tmpfs"]

    def test_the_api_waits_for_a_successful_init_and_init_waits_for_a_healthy_database(self, compose):
        assert compose["services"]["clinical-api"]["depends_on"]["clinical-init"] == {
            "condition": "service_completed_successfully"
        }
        assert compose["services"]["clinical-init"]["depends_on"]["clinical-db"] == {
            "condition": "service_healthy",
            "required": False,
        }
        assert compose["services"]["clinical-init"]["restart"] == "no"

    def test_init_runs_roles_then_migrations_then_verification_and_stops_on_the_first_failure(self, compose):
        command = compose["services"]["clinical-init"]["command"]
        assert command[:2] == ["sh", "-c"]
        steps = [s.strip() for s in command[2].split("&&")]
        assert steps == [
            "python -m clinical.bootstrap",
            "python -m clinical.migrate --migrate",
            "python -m clinical.migrate --verify",
        ]
        assert "--apply-schema" not in command[2]

    def test_the_test_service_mounts_the_checkout_read_only_and_waits_for_its_database(self, compose):
        service = compose["services"]["clinical-test"]
        (mount,) = service["volumes"]
        assert mount["target"] == "/repo" and mount["read_only"] is True
        assert service["depends_on"]["clinical-test-db"] == {"condition": "service_healthy"}
        assert (
            service["build"]["target"] == "test" and compose["services"]["clinical-api"]["build"]["target"] == "runtime"
        )

    def test_the_provision_job_cannot_be_started_by_a_plain_up(self, compose):
        assert compose["services"]["clinical-provision"]["profiles"] == ["ops"]
        assert compose["services"]["clinical-provision"]["entrypoint"][-3:] == ["python", "-m", "clinical.provision"]

    def test_the_bundled_databases_are_pinned_by_digest(self, compose):
        for name in ("clinical-db", "clinical-test-db"):
            assert re.search(r"@sha256:[0-9a-f]{64}\}$", compose["services"][name]["image"]), name

    def test_every_clinical_service_has_bounded_logs(self, compose):
        for name in ("clinical-db", "clinical-init", "clinical-api", "clinical-provision"):
            logging = compose["services"][name]["logging"]
            assert logging["driver"] == "json-file" and logging["options"] == {"max-size": "10m", "max-file": "5"}, name


# ═════════════════════════════════════════════════════════════════════════════
# THE DOCUMENT, AND NOTHING POINTING AT A FILE THAT DOES NOT EXIST
# ═════════════════════════════════════════════════════════════════════════════

DEPLOYMENT_FILES = (
    CLINICAL / "Dockerfile.slim",
    CLINICAL / "Dockerfile.slim.dockerignore",
    CLINICAL / "docker-entrypoint.sh",
    CLINICAL / "requirements.txt",
    CLINICAL / "requirements.lock",
    CLINICAL / "requirements-dev.lock",
    COMPOSE,
    ENV_EXAMPLE,
    SCRIPT,
)


class TestNoDanglingReferences:
    @pytest.mark.parametrize("path", DEPLOYMENT_FILES, ids=lambda p: p.name)
    def test_every_document_and_script_a_deployment_file_names_exists(self, path):
        text = path.read_text(encoding="utf-8")
        for ref in re.findall(r"\b(?:docs|scripts)/[A-Za-z0-9_.\-]+\.(?:md|sh)\b", text):
            assert (REPO / ref).is_file(), f"{path.name} refers to {ref}, which does not exist"
        for ref in re.findall(r"(?<![\w/.-])(\.env\.clinical\.example|docker-compose\.clinical\.yml)\b", text):
            assert (REPO / ref).is_file(), f"{path.name} refers to {ref}, which does not exist"

    def test_the_deployment_document_exists_and_is_linked_from_the_docker_guide(self):
        assert DOC.is_file()
        assert "CLINICAL_DEPLOYMENT.md" in (REPO / "DOCKER.md").read_text(encoding="utf-8")

    def test_the_document_refers_only_to_files_that_exist(self):
        text = DOC.read_text(encoding="utf-8")
        for ref in re.findall(r"`((?:clinical|scripts|docs)/[A-Za-z0-9_.\-/]+\.[a-z]+)`", text):
            assert (REPO / ref).exists(), f"CLINICAL_DEPLOYMENT.md refers to {ref}"
        for ref in re.findall(r"`(docker-compose\.clinical\.yml|\.env\.clinical\.example|DOCKER\.md)`", text):
            assert (REPO / ref).exists(), ref


class TestTheDeploymentDocumentStatesWhatItDoesNotDo:
    def _text(self):
        return DOC.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "heading",
        [
            "Not production-ready",
            "Quick start",
            "Secrets",
            "Database initialisation and migrations",
            "First administrator",
            "HTTPS and reverse proxy",
            "Multi-factor authentication key",
            "Backup and restore",
            "Recovery limitations",
            "Logs and privacy",
            "Workers and the login throttle",
            "Running the tests",
            "Unresolved risks",
        ],
    )
    def test_the_required_sections_are_present(self, heading):
        assert re.search(rf"^#+ .*{re.escape(heading)}", self._text(), re.M), heading

    def test_it_says_in_so_many_words_what_is_missing_for_production(self):
        text = self._text().lower()
        for needle in ("https", "backup", "vulnerability scan", "hash", "retention"):
            assert needle in text, needle
        assert "not production-ready" in text and "is production-ready" not in text.replace("not production-ready", "")

    def test_it_never_shows_a_wildcard_proxy_setting_as_something_to_use(self):
        text = self._text()
        assert not re.search(r"FORWARDED_ALLOW_IPS\s*=\s*\*", text)
        assert "never `*`" in text.lower() or "never '*'" in text.lower() or "never *" in text.lower()

    def test_it_uses_the_component_naming_the_repository_pins(self):
        assert not re.search(r"Bij AI(?!\s+(?:variant-interpretation|sequencing-analysis) component)", self._text())
