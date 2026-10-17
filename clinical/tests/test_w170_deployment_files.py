"""
clinical/tests/test_w170_deployment_files.py
────────────────────────────────────────────
Stage 4, increment 1: the deployment artefacts that make the clinical service an
image -- `clinical/Dockerfile.slim`, its ignore file, the entrypoint, the pinned
requirements and their locks, the `.gitignore` rules for secrets, and the CI
wiring -- are held to the properties they were written to have.

NO DOCKER AND NO DATABASE IS NEEDED. Every row reads a file or runs the entrypoint
script under `sh`, so these run on a laptop, in CI, and inside the `test` target.
What they cannot show -- that the image builds, runs non-root, and answers
/readyz -- is shown by building and running it (docs/CLINICAL_DEPLOYMENT.md),
and the build results are recorded in the commit that introduced them, not here.

`clinical/Dockerfile` (the pipeline-based image) is deliberately untouched, and
test_w139 still pins it: uvicorn, `clinical.app:app`, and no TLS arguments.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

CLINICAL = pathlib.Path(__file__).resolve().parent.parent
REPO = CLINICAL.parent

SLIM = CLINICAL / "Dockerfile.slim"
SLIM_IGNORE = CLINICAL / "Dockerfile.slim.dockerignore"
ENTRYPOINT = CLINICAL / "docker-entrypoint.sh"

SECRET_VARIABLES = (
    "CLINICAL_DSN",
    "CLINICAL_BOOTSTRAP_DSN",
    "CLINICAL_APP_PASSWORD",
    "CLINICAL_RETENTION_PASSWORD",
    "CLINICAL_TOTP_SECRET_KEYS",
    "CLINICAL_FIRST_ADMIN_PASSWORD",
)


def _instructions(path: pathlib.Path):
    """Dockerfile instructions with comments dropped and line continuations joined."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"\\\n", " ", text)
    return [line.strip() for line in text.split("\n") if line.strip() and not line.strip().startswith("#")]


def _stages(path: pathlib.Path):
    """{stage name: [instructions]} in file order."""
    stages, current = {}, None
    for line in _instructions(path):
        if line.upper().startswith("FROM "):
            match = re.search(r"\sAS\s+(\S+)\s*$", line, re.I)
            current = match.group(1) if match else f"stage{len(stages)}"
            stages[current] = [line]
        elif current is not None:
            stages[current].append(line)
    return stages


def _final_user(stage_lines, inherited="root"):
    user = inherited
    for line in stage_lines:
        if line.upper().startswith("USER "):
            user = line.split(None, 1)[1].strip()
    return user


def _requirement_lines(path: pathlib.Path):
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lines):
    """{normalised name: version} for `name==version` / `name[extra]==version` lines."""
    pins = {}
    for line in lines:
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==([A-Za-z0-9_.\-+!]+)", line)
        assert match, f"not an exact pin: {line!r}"
        pins[_normal(match.group(1))] = match.group(3)
    return pins


# ═════════════════════════════════════════════════════════════════════════════
# THE IMAGE
# ═════════════════════════════════════════════════════════════════════════════


class TestTheSlimDockerfile:
    def test_the_base_image_is_pinned_by_digest(self):
        text = SLIM.read_text(encoding="utf-8")
        match = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", text, re.M)
        assert match, "no ARG PYTHON_IMAGE default"
        assert re.fullmatch(r"python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}", match.group(1)), match.group(1)

    def test_every_stage_starts_from_that_pinned_argument_or_an_earlier_stage(self):
        stages = _stages(SLIM)
        assert set(stages) == {"deps", "runtime", "test"}, list(stages)
        for name, lines in stages.items():
            base = lines[0].split()[1]
            assert base in ("${PYTHON_IMAGE}", "runtime"), (name, base)

    def test_no_stage_ends_as_root(self):
        stages = _stages(SLIM)
        runtime_user = _final_user(stages["runtime"])
        assert runtime_user == "10001:10001", runtime_user
        # the test stage switches to root to install, and must switch back
        assert _final_user(stages["test"], inherited=runtime_user) == "10001:10001"

    def test_the_service_user_has_no_shell_and_no_home(self):
        text = " ".join(_stages(SLIM)["runtime"])
        assert "--uid 10001" in text and "--no-create-home" in text and "/usr/sbin/nologin" in text

    def test_it_launches_uvicorn_on_the_app_with_one_worker_and_no_tls_arguments(self):
        cmd = next(line for line in _stages(SLIM)["runtime"] if line.startswith("CMD "))
        assert '"uvicorn"' in cmd and '"clinical.app:app"' in cmd, cmd
        assert '"--workers", "1"' in cmd, cmd
        for tls in ("--ssl-certfile", "--ssl-keyfile", "--ssl-ca-certs"):
            assert tls not in SLIM.read_text(encoding="utf-8")

    def test_it_never_trusts_forwarded_headers_from_everyone(self):
        text = SLIM.read_text(encoding="utf-8")
        assert not re.search(r"forwarded-allow-ips[ =]+['\"]?\*", text, re.I)
        assert not re.search(r"FORWARDED_ALLOW_IPS\s*=\s*['\"]?\*", text)
        # and sets no allow-list at all: the default trusts 127.0.0.1 only, and a
        # deployment names its proxy explicitly
        assert "--forwarded-allow-ips" not in text.split("CMD", 1)[1].split("\n", 1)[0]

    def test_the_healthcheck_probes_readiness_not_just_liveness(self):
        health = next(line for line in _stages(SLIM)["runtime"] if line.startswith("HEALTHCHECK"))
        assert "/readyz" in health and "/healthz" not in health, health

    def test_it_installs_only_from_the_lock_and_checks_consistency(self):
        deps = " ".join(_stages(SLIM)["deps"])
        assert "pip install --no-deps -r requirements.lock" in deps
        assert "pip check" in deps

    def test_the_tests_and_the_test_tools_cannot_reach_the_runtime_stage(self):
        runtime = _stages(SLIM)["runtime"]
        copies = [line for line in runtime if line.upper().startswith("COPY ")]
        assert copies, "no COPY lines"
        for line in copies:
            assert "clinical/tests" not in line, line
            assert not re.search(r"COPY\s+clinical\s", line), f"copies the whole directory: {line}"
            assert "requirements-dev" not in line, line
        assert not any("pytest" in line or "httpx" in line for line in runtime)

    def test_the_test_stage_runs_the_mounted_checkout_not_a_baked_copy(self):
        test = " ".join(_stages(SLIM)["test"])
        assert "WORKDIR /repo" in test and "PYTHONPATH=/repo" in test

    def test_no_secret_is_baked_into_the_image_definition(self):
        for line in _instructions(SLIM):
            if line.split(None, 1)[0].upper() in ("ENV", "ARG"):
                for name in SECRET_VARIABLES:
                    assert name not in line, f"{name} appears in an ENV/ARG line: {line}"
        assert "password" not in " ".join(_instructions(SLIM)).lower().replace("--no-create-home", "")

    def test_only_the_service_port_is_exposed(self):
        exposed = [line for line in _stages(SLIM)["runtime"] if line.upper().startswith("EXPOSE")]
        assert exposed == ["EXPOSE 8000"], exposed

    def test_the_older_pipeline_based_dockerfile_is_still_there_and_still_unchanged_in_kind(self):
        old = (CLINICAL / "Dockerfile").read_text(encoding="utf-8")
        assert "FROM ${BASE_IMAGE}" in old and "clinical.app:app" in old


class TestTheBuildContextIsAnAllowList:
    def _rules(self):
        return [
            line.strip()
            for line in SLIM_IGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_everything_is_excluded_first(self):
        assert self._rules()[0] == "**"

    def test_only_service_files_are_let_back_in(self):
        allowed = [rule[1:] for rule in self._rules() if rule.startswith("!")]
        assert allowed, "nothing is allowed back in"
        for pattern in allowed:
            assert pattern.startswith("clinical/"), pattern
            assert "tests" not in pattern and ".env" not in pattern and "secret" not in pattern, pattern

    def test_tests_and_bytecode_are_excluded_after_the_allow_rules(self):
        rules = self._rules()
        last_allow = max(i for i, rule in enumerate(rules) if rule.startswith("!"))
        for must_exclude in ("clinical/tests", "**/__pycache__"):
            assert must_exclude in rules and rules.index(must_exclude) > last_allow, must_exclude


# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCIES
# ═════════════════════════════════════════════════════════════════════════════


class TestDependenciesArePinned:
    DIRECT = ("fastapi", "uvicorn", "psycopg", "bcrypt", "pyotp", "cryptography")

    def test_the_runtime_requirements_are_exact_pins_of_exactly_the_service_needs(self):
        pins = _pins(_requirement_lines(CLINICAL / "requirements.txt"))
        assert set(pins) == set(self.DIRECT), sorted(pins)

    def test_the_runtime_requirements_carry_no_test_tool(self):
        names = " ".join(_requirement_lines(CLINICAL / "requirements.txt")).lower()
        assert "pytest" not in names and "httpx" not in names

    def test_the_dev_requirements_add_exactly_the_test_tools_on_top_of_the_runtime_set(self):
        lines = _requirement_lines(CLINICAL / "requirements-dev.txt")
        assert lines[0] == "-r requirements.txt"
        assert set(_pins(lines[1:])) == {"pytest", "httpx"}

    def test_cryptography_stays_declared_for_the_mfa_tripwire(self):
        # test_w164_mfa::test_cryptography_is_a_declared_dependency reads this file
        lines = (CLINICAL / "requirements.txt").read_text(encoding="utf-8").splitlines()
        assert any(line.strip().startswith("cryptography") for line in lines)

    @pytest.mark.parametrize(
        "lock, source, extra",
        [
            ("requirements.lock", "requirements.txt", ()),
            ("requirements-dev.lock", "requirements-dev.txt", ("pytest", "httpx")),
        ],
    )
    def test_each_lock_pins_every_package_and_agrees_with_its_source(self, lock, source, extra):
        locked = _pins(_requirement_lines(CLINICAL / lock))
        direct = _pins([line for line in _requirement_lines(CLINICAL / source) if not line.startswith("-r")])
        for name, version in direct.items():
            assert locked.get(name) == version, (lock, name, version, locked.get(name))
        assert "psycopg-binary" in locked and locked["psycopg-binary"] == locked["psycopg"]
        assert len(locked) > len(direct), "a lock with no transitive packages is not a lock"

    def test_the_dev_lock_contains_the_whole_runtime_lock(self):
        runtime = _pins(_requirement_lines(CLINICAL / "requirements.lock"))
        dev = _pins(_requirement_lines(CLINICAL / "requirements-dev.lock"))
        assert {name: dev.get(name) for name in runtime} == runtime

    def test_the_locks_name_the_base_image_they_were_generated_on(self):
        digest = re.search(r"python@?:?3\.12-slim-bookworm@(sha256:[0-9a-f]{64})", SLIM.read_text(encoding="utf-8"))
        for lock in ("requirements.lock", "requirements-dev.lock"):
            assert digest.group(1) in (CLINICAL / lock).read_text(encoding="utf-8"), lock


# ═════════════════════════════════════════════════════════════════════════════
# GIT AND CI
# ═════════════════════════════════════════════════════════════════════════════


class TestSecretsCannotBeCommittedAndCiInstallsTheLock:
    def _gitignore(self):
        return (REPO / ".gitignore").read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")

    def test_the_secret_paths_are_ignored(self):
        lines = self._gitignore()
        for rule in ("/secrets/", "/secrets/**", ".env.clinical"):
            assert rule in lines, rule

    def test_the_example_file_is_re_included_after_the_broad_env_rule(self):
        lines = self._gitignore()
        assert ".env.*" in lines and "!.env.clinical.example" in lines
        assert lines.index("!.env.clinical.example") > lines.index(".env.*")

    def test_ci_installs_the_dev_lock_for_the_clinical_job(self):
        workflow = (REPO / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
        job = workflow.split("\n  clinical-tests:\n", 1)[1].split("\n  shared-tests:\n", 1)[0]
        assert "pip install -r requirements-dev.lock" in job
        assert "cache-dependency-path: clinical/requirements-dev.lock" in job
        assert "pip install -r requirements.txt" not in job


# ═════════════════════════════════════════════════════════════════════════════
# THE ENTRYPOINT
# ═════════════════════════════════════════════════════════════════════════════


requires_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on this machine")


class TestTheEntrypointSource:
    def test_it_is_lf_only_so_a_windows_checkout_cannot_break_it(self):
        assert b"\r" not in ENTRYPOINT.read_bytes()

    def test_it_reads_exactly_the_listed_variables_and_nothing_else(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        block = re.search(r'SECRET_VARIABLES="\n(.*?)"', text, re.S).group(1).split()
        assert tuple(block) == SECRET_VARIABLES

    def test_it_fails_on_error_and_hands_over_to_the_command(self):
        lines = [line for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        assert "set -eu" in lines and lines[-1] == 'exec "$@"'

    def test_it_never_prints_a_secret_value(self):
        for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("echo", "printf")):
                assert "$value" not in line and "$current" not in line, line


@requires_sh
class TestTheEntrypointBehaviour:
    SECRET = "s3cr3t-value-never-to-be-printed"

    def _run(self, tmp_path, env, command=None):
        command = command or ["sh", "-c", 'printf "%s|%s" "${CLINICAL_DSN-UNSET}" "${CLINICAL_DSN_FILE-UNSET}"']
        full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin"} | env
        import os

        if os.name == "nt":  # Git for Windows' sh needs its own PATH to find cat/printf
            full_env["PATH"] = os.environ["PATH"]
        return subprocess.run(
            ["sh", str(ENTRYPOINT), *command], capture_output=True, text=True, env=full_env, timeout=30, cwd=tmp_path
        )

    def _file(self, tmp_path, content, name="secret"):
        path = tmp_path / name
        path.write_bytes(content.encode())
        return str(path).replace("\\", "/")

    def test_a_secret_file_becomes_the_variable_and_the_file_variable_is_not_leaked(self, tmp_path):
        result = self._run(tmp_path, {"CLINICAL_DSN_FILE": self._file(tmp_path, self.SECRET + "\n")})
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{self.SECRET}|UNSET"  # newline stripped, *_FILE removed

    def test_with_no_file_variable_the_environment_is_left_alone(self, tmp_path):
        result = self._run(tmp_path, {"CLINICAL_DSN": "from-env"})
        assert result.stdout == "from-env|UNSET" and result.returncode == 0

    def test_nothing_is_invented_when_nothing_is_set(self, tmp_path):
        result = self._run(tmp_path, {})
        assert result.stdout == "UNSET|UNSET" and result.returncode == 0

    def test_both_the_variable_and_its_file_set_is_refused_without_printing_either(self, tmp_path):
        result = self._run(tmp_path, {"CLINICAL_DSN": "inline", "CLINICAL_DSN_FILE": self._file(tmp_path, self.SECRET)})
        assert result.returncode == 64
        assert "CLINICAL_DSN" in result.stderr
        assert self.SECRET not in result.stdout + result.stderr and "inline" not in result.stdout + result.stderr

    def test_a_missing_file_is_refused(self, tmp_path):
        result = self._run(tmp_path, {"CLINICAL_DSN_FILE": str(tmp_path / "nope").replace("\\", "/")})
        assert result.returncode == 66 and "CLINICAL_DSN_FILE" in result.stderr

    def test_an_empty_file_is_refused(self, tmp_path):
        result = self._run(tmp_path, {"CLINICAL_DSN_FILE": self._file(tmp_path, "\n")})
        assert result.returncode == 66 and "empty" in result.stderr

    def test_a_file_variable_that_is_not_on_the_list_is_never_followed(self, tmp_path):
        command = ["sh", "-c", 'printf "%s" "${SOMETHING_ELSE-UNSET}"']
        result = self._run(tmp_path, {"SOMETHING_ELSE_FILE": self._file(tmp_path, self.SECRET)}, command)
        assert result.stdout == "UNSET" and result.returncode == 0

    def test_the_command_is_exec_ed_with_its_own_exit_status(self, tmp_path):
        result = self._run(tmp_path, {}, ["sh", "-c", "exit 7"])
        assert result.returncode == 7

    def test_every_listed_variable_can_be_loaded_from_a_file(self, tmp_path):
        for name in SECRET_VARIABLES:
            command = ["sh", "-c", f'printf "%s" "${{{name}-UNSET}}"']
            result = self._run(tmp_path, {f"{name}_FILE": self._file(tmp_path, f"value-for-{name}\n", name)}, command)
            assert result.stdout == f"value-for-{name}", (name, result.stderr)
