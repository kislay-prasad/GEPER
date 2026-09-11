"""
Kim's non-report surfaces name the COMPONENT, not the whole product.

Human's rule, 2026-09-11: "The defect was never the words 'Bij AI' -- it was
a component claiming to be the whole product." Anything that announces or
describes what is running here is scoped to "Bij AI sequencing-analysis
component"; the ratified intended-use disclaimer, which names the PRODUCT,
is left alone and pinned below.

Measured before the change (28db140), not assumed:
  * `main.py --help` described Kim with GEPER's own expansion ("Genomic
    Evidence Pipeline with Evidence-based Risk assessment");
  * `python -m pipeline.orchestration.runner --help`: "Bij AI genomic
    pipeline: FASTQ -> QC -> ...";
  * `serve_api.py --help`: "Bij AI genomics pipeline API server", and its
    startup line "Starting Bij AI API on http://..." -- it launches
    api.main:app, as does `main.py serve` ("Starting Bij AI API server on");
  * api/main.py's FastAPI app imports only Kim's PipelineRunner and serves
    only FASTQ/FASTA upload + Kim pipeline runs, yet was titled "Bij AI
    Genomics Pipeline API".

Every string is compared against the constants the reports use
(pipeline/reporting/component_identity.py, re-exported by stage.py and
clinical_sections.py), so these surfaces cannot drift from the reports.
The servers are started with a stand-in `uvicorn` module that records its
arguments and returns -- no port is ever opened.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.reporting.clinical_sections import SCOPE_LINE
from pipeline.reporting.component_identity import COMPONENT_NAME
from pipeline.reporting.stage import PIPELINE_VERSION

_KIM_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _KIM_ROOT.parent
# "Bij AI" naming neither component -- the whole-product claim.
_BARE_NAME = re.compile(
    r"Bij AI(?! sequencing-analysis component| variant-interpretation component)"
)
_GEPER_EXPANSION = ("Genomic Evidence Pipeline", "Evidence-based Risk")


def _run(args, *, extra_path=(), cwd=_KIM_ROOT):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("GEPER_DEV_INSECURE", "1")
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in extra_path)
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _flat(text):
    return " ".join(text.split())


@pytest.fixture
def uvicorn_stand_in(tmp_path):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "uvicorn.py").write_text(
        "def run(app, **kw):\n    print('[uvicorn stand-in] app=' + str(app))\n",
        encoding="utf-8",
    )
    return stub_dir


# ── (1) main.py --help: the scope sentence, not GEPER's expansion ────────────


def test_main_help_is_component_name_then_the_report_scope_sentence():
    out = _run([str(_KIM_ROOT / "main.py"), "--help"])
    for phrase in _GEPER_EXPANSION:
        assert phrase not in out
    lines = [line.rstrip() for line in out.splitlines()]
    at = lines.index(PIPELINE_VERSION)
    assert lines[at + 1] == SCOPE_LINE


def test_main_module_docstring_names_no_geper_workflow():
    import main

    assert "GEPER primary workflow" not in main.__doc__
    assert "GEPER test suite" not in main.__doc__
    assert f"Run the {COMPONENT_NAME} test suite" in main.__doc__


# ── (2) runner / serve_api help, and both servers' startup lines ─────────────


def test_runner_help_names_the_component():
    out = _flat(_run(["-m", "pipeline.orchestration.runner", "--help"], extra_path=[_REPO_ROOT]))
    assert out.count(PIPELINE_VERSION) >= 1
    assert _BARE_NAME.findall(out) == []
    assert "the Bij AI variant-interpretation component (GEPER)" in out


def test_serve_api_help_names_the_component():
    out = _flat(_run([str(_KIM_ROOT / "serve_api.py"), "--help"]))
    assert f"{COMPONENT_NAME} API server" in out
    assert _BARE_NAME.findall(out) == []


def test_serve_api_startup_line_names_the_component(uvicorn_stand_in):
    out = _run([str(_KIM_ROOT / "serve_api.py")], extra_path=[uvicorn_stand_in, _REPO_ROOT])
    assert "[uvicorn stand-in] app=api.main:app" in out  # it would serve Kim's API; no port opened
    assert f"Starting the {COMPONENT_NAME} API on http://127.0.0.1:8000" in out
    assert _BARE_NAME.findall(out) == []


def test_main_serve_startup_line_names_the_component(uvicorn_stand_in):
    out = _run([str(_KIM_ROOT / "main.py"), "serve"], extra_path=[uvicorn_stand_in, _REPO_ROOT])
    assert "[uvicorn stand-in] app=api.main:app" in out
    assert f"Starting the {COMPONENT_NAME} API server on 127.0.0.1:8000" in out
    assert _BARE_NAME.findall(out) == []


# ── (3) the Kim API's OpenAPI identity ───────────────────────────────────────


def test_openapi_title_and_description_name_the_component():
    pytest.importorskip("fastapi")
    from api.main import app

    info = app.openapi()["info"]
    assert info["title"] == f"{COMPONENT_NAME} API"
    assert info["description"].startswith(
        f"Production API for the {COMPONENT_NAME} (Kim). Accepts FASTQ inputs and returns annotated variant reports."
    )
    assert "Bij AI clinical genomics pipeline" not in info["description"]


# ── (4) the LEFT disclaimer is byte-unchanged (the leave decision, pinned) ───

_INTENDED_USE_REPORT = (
    "This report is generated by Bij AI, an in-development bioinformatics pipeline "
    "that assists qualified clinicians and pathologists. It produces a draft "
    "classification requiring qualified human review and final sign-off before "
    "any clinical use, and does not independently provide final clinical "
    "interpretation."
)
_INTENDED_USE_API = (
    "DISCLAIMER: Bij AI assists qualified clinicians and pathologists; it produces a draft "
    "classification requiring qualified human review and final sign-off before any clinical "
    "use, and does not independently provide final clinical interpretation."
)


def test_report_disclaimer_is_unchanged_on_both_kim_renderers(tmp_path):
    # Rendered, via the real defaults: the PDF's footer carries the first
    # 150 characters, the HTML carries all of it.
    from tests.test_component_scope_names import _run_stage

    html = Path(_run_stage(tmp_path).html_path).read_text()
    assert _INTENDED_USE_REPORT in html

    import inspect

    from pipeline.reporting import pdf_report

    src = inspect.getsource(pdf_report.render_clinical_pdf)
    joined = "".join(
        re.findall(
            r'"((?:[^"\\]|\\.)*)"', src[src.index("lab_disclaimer = lab_disclaimer or (") :]
        )[:5]
    )
    assert joined == _INTENDED_USE_REPORT


def test_api_disclaimer_is_unchanged():
    pytest.importorskip("fastapi")
    from api.main import app

    assert app.openapi()["info"]["description"].endswith(_INTENDED_USE_API)
