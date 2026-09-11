"""
Kim's CLI calls itself by its own console-script name, not "geper".

Human-approved 2026-09-12 ("rename it"). argparse `prog` sets only the text
argparse prints: the usage line and the "<prog>: error:" prefix. It was
"geper", the platform's name, though the console script that installs
this CLI has been `kim-pipeline` since 2026-09-09 (c175e14) and nothing in
the repository invokes Kim as `geper` (measured: every invocation is
`python main.py ...` or the console script). So a user of `kim-pipeline
--help` was told the program was called `geper`.

The expected name is read from pyproject.toml's [project.scripts] entry
that points at this CLI (main:cli), so the usage text cannot drift from the
command that actually installs it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

_KIM_ROOT = Path(__file__).resolve().parents[1]


def _console_script_name() -> str:
    scripts = tomllib.loads((_KIM_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    names = [name for name, target in scripts.items() if target == "main:cli"]
    assert len(names) == 1, scripts
    return names[0]


def _run(*args: str):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(_KIM_ROOT / "main.py"), *args],
        cwd=str(_KIM_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_console_script_is_kim_pipeline():
    # Control: the name the CLI must print is the installed command's.
    assert _console_script_name() == "kim-pipeline"


def test_help_usage_line_names_the_console_script():
    code, out, _err = _run("--help")
    assert code == 0
    assert out.splitlines()[0].startswith(f"usage: {_console_script_name()} ")
    assert "usage: geper" not in out


def test_error_prefix_names_the_console_script():
    # No subcommand: argparse's own "required" error, printed with prog.
    code, _out, err = _run()
    assert code == 2
    assert f"{_console_script_name()}: error:" in err
    assert "usage: geper" not in err
    assert "geper: error" not in err


def test_subcommand_error_prefix_names_the_console_script():
    code, _out, err = _run("analyze")  # missing its required arguments
    assert code == 2
    assert f"{_console_script_name()} analyze: error:" in err
    assert "geper" not in err.split(":")[0]
