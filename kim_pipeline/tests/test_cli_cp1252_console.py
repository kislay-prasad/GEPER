"""
Kim's CLIs must run on a default Windows console (cp1252).

Found 2026-09-11: `py -3.12 kim_pipeline/main.py --help` died with
UnicodeEncodeError on U+2192 "→" before printing anything. Human ruling:
use "->", and do NOT switch console output to UTF-8 ("fixes it on machines
that cooperate and fails on the ones that don't"). So every string these
CLIs print must be encodable to cp1252 -- em dash U+2014 is, and stays.

Two checks:
  * RUN: each CLI's --help, `main.py validate`, the verify-environment
    report, and both server startup lines, as subprocesses with
    PYTHONIOENCODING=cp1252 -- exit status plus the expected text. The
    servers get a stand-in `uvicorn` that records its arguments and
    returns; no port is opened.
  * SOURCE: every non-docstring string literal in the CLI modules is
    actually encoded to cp1252, so a print() on a branch no run reaches
    cannot reintroduce the crash.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.reporting.component_identity import COMPONENT_NAME, PIPELINE_VERSION

_KIM_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _KIM_ROOT.parent
_CLI_MODULES = (
    "main.py",
    "pipeline/orchestration/runner.py",
    "serve_api.py",
    "verify_environment.py",
    "pipeline/utils/dependency_validator.py",
    "pipeline/reporting/component_identity.py",
)
_MAIN_SUBCOMMANDS = (
    "analyze",
    "vcf",
    "classify",
    "serve",
    "validate",
    "verify-environment",
    "test",
)


def _run_cp1252(args, *, extra_path=(_REPO_ROOT,)):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "cp1252"
    env.setdefault("GEPER_DEV_INSECURE", "1")
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in extra_path)
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(_KIM_ROOT),
        env=env,
        capture_output=True,
        timeout=180,
    )
    # Decode as the console would have had to encode it.
    return proc.returncode, proc.stdout.decode("cp1252"), proc.stderr.decode("cp1252", "replace")


@pytest.fixture
def uvicorn_stand_in(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "uvicorn.py").write_text(
        "def run(app, **kw):\n    print('[uvicorn stand-in] app=' + str(app))\n", encoding="utf-8"
    )
    return stub


# ── RUN ───────────────────────────────────────────────────────────────────────


def test_main_help_on_cp1252():
    code, out, err = _run_cp1252([str(_KIM_ROOT / "main.py"), "--help"])
    assert code == 0, err
    assert PIPELINE_VERSION in out
    assert "Full FASTQ -> Report pipeline" in out


@pytest.mark.parametrize("sub", _MAIN_SUBCOMMANDS)
def test_main_subcommand_help_on_cp1252(sub):
    code, out, err = _run_cp1252([str(_KIM_ROOT / "main.py"), sub, "--help"])
    assert code == 0, err
    # Was "usage: geper {sub}": prog renamed to the console-script name 2026-09-12.
    assert f"usage: kim-pipeline {sub}" in out


def test_runner_help_on_cp1252():
    code, out, err = _run_cp1252(["-m", "pipeline.orchestration.runner", "--help"])
    assert code == 0, err
    flat = " ".join(out.split())
    assert (
        f"{PIPELINE_VERSION}: FASTQ -> QC -> Alignment -> Variant Calling -> Annotation -> Report"
        in flat
    )


def test_serve_api_help_on_cp1252():
    code, out, err = _run_cp1252([str(_KIM_ROOT / "serve_api.py"), "--help"])
    assert code == 0, err
    assert f"{COMPONENT_NAME} API server" in out


def test_main_validate_on_cp1252():
    code, out, err = _run_cp1252([str(_KIM_ROOT / "main.py"), "validate"])
    assert code == 0, err
    assert "[OK] Config is valid: config/default.yaml" in out


def test_verify_environment_report_on_cp1252():
    # Exit 1 is a legitimate FAIL verdict (e.g. bwa absent on this machine);
    # what must not happen is an encoding crash before or while printing it.
    code, out, err = _run_cp1252([str(_KIM_ROOT / "verify_environment.py")])
    assert code in (0, 1), err
    assert "UnicodeEncodeError" not in err
    assert f"{COMPONENT_NAME} Environment Verification" in out
    assert "Overall:" in out


def test_serve_api_startup_on_cp1252(uvicorn_stand_in):
    code, out, err = _run_cp1252(
        [str(_KIM_ROOT / "serve_api.py")], extra_path=(uvicorn_stand_in, _REPO_ROOT)
    )
    assert code == 0, err
    assert f"Starting the {COMPONENT_NAME} API on http://127.0.0.1:8000" in out
    assert "[uvicorn stand-in] app=api.main:app" in out


def test_main_serve_startup_on_cp1252(uvicorn_stand_in):
    code, out, err = _run_cp1252(
        [str(_KIM_ROOT / "main.py"), "serve"], extra_path=(uvicorn_stand_in, _REPO_ROOT)
    )
    assert code == 0, err
    assert f"Starting the {COMPONENT_NAME} API server on 127.0.0.1:8000" in out


# ── SOURCE ────────────────────────────────────────────────────────────────────


def _non_docstring_literals(rel):
    tree = ast.parse((_KIM_ROOT / rel).read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and body
        ):
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def test_every_cli_literal_encodes_to_cp1252():
    hits = []
    for rel in _CLI_MODULES:
        for lineno, value in _non_docstring_literals(rel):
            try:
                value.encode("cp1252")
            except UnicodeEncodeError as exc:
                hits.append(f"{rel}:{lineno}: {value[exc.start]!r} (U+{ord(value[exc.start]):04X})")
    assert hits == []
