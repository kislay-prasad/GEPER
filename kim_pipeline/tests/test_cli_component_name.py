"""
Kim's CLI must name the component exactly as its reports do.

Human-approved 2026-09-11 ("Kim's CLI: change it"): after the naming ruling
(Option B) every kim report says "Bij AI sequencing-analysis component v8",
while `main.py --version` still printed "Bij AI v8.0.0" and the help
description "Bij AI v8 — ..." -- the same whole-product claim the ruling
removed from the reports, on the surface an integrator reads first.

The string is checked against the REPORT constant
(pipeline/reporting/stage.py::PIPELINE_VERSION), so the CLI and the reports
cannot drift. `--version` and `--help` are run as a real subprocess with no
repo root on PYTHONPATH, so a check that passes here also proves the
version/help path needs nothing beyond kim_pipeline itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.reporting.stage import PIPELINE_VERSION

_KIM_ROOT = Path(__file__).resolve().parents[1]
# "Bij AI" naming neither component -- the whole-product claim.
_BARE_NAME = re.compile(
    r"Bij AI(?! sequencing-analysis component| variant-interpretation component)"
)


def _run_cli(*args: str) -> str:
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
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _parser():
    import main

    return main, main._build_parser()


def test_the_report_constant_is_the_ruled_name():
    # Control: the thing the CLI is compared against is itself the ruled string.
    assert PIPELINE_VERSION == "Bij AI sequencing-analysis component v8"


def test_version_prints_the_report_constant():
    out = _run_cli("--version").strip()
    assert out == PIPELINE_VERSION
    assert "Bij AI v8" not in out


def test_top_level_help_names_the_component():
    out = " ".join(_run_cli("--help").split())
    assert PIPELINE_VERSION in out
    assert "Bij AI v8" not in out
    assert _BARE_NAME.findall(out) == []


def test_every_subcommand_help_names_a_component_not_the_whole_product():
    _main, parser = _parser()
    subparsers = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    texts = {"<top>": parser.format_help()}
    texts.update({name: sub.format_help() for name, sub in subparsers.choices.items()})
    texts.update({f"<summary:{c.dest}>": c.help or "" for c in subparsers._choices_actions})
    # argparse wraps help text; a name split across a line break is still the name.
    texts = {name: " ".join(text.split()) for name, text in texts.items()}
    offenders = {
        name: _BARE_NAME.findall(text) for name, text in texts.items() if _BARE_NAME.search(text)
    }
    assert offenders == {}
    assert not any("Bij AI v8" in text for text in texts.values())


def test_module_docstring_names_the_component():
    main, _parser_obj = _parser()
    assert PIPELINE_VERSION in (main.__doc__ or "")
    assert "Bij AI v8" not in (main.__doc__ or "")
