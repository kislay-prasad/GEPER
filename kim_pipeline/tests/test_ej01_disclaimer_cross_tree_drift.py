"""
EJ-01 disclaimer drift test: Kim and GEPER must print the SAME text.

The intended-use disclaimer is written twice, once per tree:
  * Kim:   ``pipeline/reporting/component_identity.py::RESEARCH_USE_DISCLAIMER``
           (rendered into the HTML footer by ``ReportingStage.run()`` and used
           as the PDF footer default by ``pdf_report.render_clinical_pdf``);
  * GEPER: ``geper/report/clinical_report_builder.py::RESEARCH_USE_DISCLAIMER``
           (rendered by every GEPER report surface).
Nothing else keeps them aligned. The human ruled (Option 1): Kim adopts
GEPER's text, and "if the texts are meant to be identical, the test pins all
of it" -- so this compares the WHOLE strings, not a shared clause.

WHY TWO SUBPROCESSES, NOT ONE IMPORT (the human's rule: "it must run each tree
separately"):
  * The two trees cannot share a process: both own top-level ``config`` and
    ``pipeline`` packages, so whichever is imported second gets the first's
    modules out of ``sys.modules``. Purging ``sys.modules`` to get around that
    would make the test pass or fail on import order, not on the text.
  * Importing two constants would pass even if neither tree could still
    PRODUCE its text. So each child runs its tree's real renderer, from that
    tree's own directory, and returns the text it actually rendered:
      - Kim: runs ``ReportingStage.run()`` for real and reads the disclaimer
        back out of the ``report.html`` it wrote (the HTML footer carries the
        full text; the PDF footer truncates it to 150 characters).
      - GEPER: runs ``ReportGenerator().generate()`` for real and reads the
        disclaimer line out of the Markdown it returned.
  * Each child also reports which file produced the text and whether any
    module from the OTHER tree was loaded, so a path mix-up cannot quietly
    compare a tree against itself.

A child that cannot produce its text FAILS this test; nothing here skips.

WHY THIS FILE LIVES IN kim_pipeline/tests (the ``kim_pipeline-tests`` CI job):
Kim's side needs only ``psutil`` (via ``shared.process_control``). GEPER's
Markdown renderer needs ``reportlab``, ``Pillow``, ``pydantic`` and
``requests``. ``kim_pipeline/requirements.txt`` declares all of them
(Pillow through reportlab), and that job also installs the repo-root
``shared`` package. ``geper/requirements.txt`` does not list ``psutil``; it
only arrives there transitively, so the geper job is the weaker host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_KIM_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _KIM_ROOT.parent
_GEPER_ROOT = _REPO_ROOT / "geper"

_MARK = "EJ01-DISCLAIMER-RESULT:"

# Runs inside kim_pipeline/. Real stage, real template, real file on disk.
_KIM_CHILD = r"""
import html, json, re, sys, tempfile
from pathlib import Path
from pipeline.reporting import stage as stage_mod

with tempfile.TemporaryDirectory() as out_dir:
    result = stage_mod.ReportingStage({"reporting": {"generate_pdf": False}}).run(
        sample_id="EJ01-DRIFT", output_dir=out_dir
    )
    # Same (platform-default) encoding the stage's own write_text() used;
    # on Windows that is cp1252, not the UTF-8 the page's <meta> declares.
    page = Path(result.html_path).read_text()

footer = re.search(r'<footer class="report-footer">\s*<p>(.*?)</p>', page, re.S)
text = html.unescape(footer.group(1)).strip() if footer else None
print(MARK + json.dumps({
    "text": text,
    "producer": stage_mod.__file__,
    "modules": [getattr(m, "__file__", None) for m in list(sys.modules.values())],
}))
"""

# Runs inside geper/. Real Markdown renderer, real output string.
_GEPER_CHILD = r"""
import json, re, sys
from report import report_generator as gen_mod

markdown = gen_mod.ReportGenerator().generate({})
line = re.search(r"^> \*\*Disclaimer:\*\* (.*)$", markdown, re.M)
text = line.group(1).strip() if line else None
print(MARK + json.dumps({
    "text": text,
    "producer": gen_mod.__file__,
    "modules": [getattr(m, "__file__", None) for m in list(sys.modules.values())],
}))
"""


def _produce(tree_root: Path, child_source: str, other_tree: Path) -> str:
    """Run one tree's real renderer in its own interpreter; return its text."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # The tree first (its own `pipeline`/`config`), then the repo root for
    # the shared/ package -- the same resolution each tree's conftest sets up.
    env["PYTHONPATH"] = os.pathsep.join([str(tree_root), str(_REPO_ROOT)])
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("GEPER_DEV_INSECURE", "1")
    proc = subprocess.run(
        [sys.executable, "-c", f"MARK = {_MARK!r}\n" + child_source],
        cwd=str(tree_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    where = f"{tree_root.name} child (exit {proc.returncode})"
    assert proc.returncode == 0, f"{where} could not render:\n{proc.stdout}\n{proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_MARK)]
    assert len(lines) == 1, f"{where} printed no result line:\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(lines[0][len(_MARK) :])

    producer = Path(payload["producer"]).resolve()
    assert producer.is_relative_to(tree_root), f"{where}: text came from {producer}, not this tree"
    foreign = sorted(
        f for f in payload["modules"] if f and Path(f).resolve().is_relative_to(other_tree)
    )
    assert foreign == [], f"{where} loaded modules from the other tree: {foreign}"

    text = payload["text"]
    assert text, f"{where} rendered a report with no disclaimer in it"
    return text


def test_kim_and_geper_render_the_identical_ej01_disclaimer():
    kim_text = _produce(_KIM_ROOT, _KIM_CHILD, other_tree=_GEPER_ROOT)
    geper_text = _produce(_GEPER_ROOT, _GEPER_CHILD, other_tree=_KIM_ROOT)
    assert kim_text == geper_text, (
        "EJ-01 disclaimer has drifted between the two trees. Update BOTH "
        "constants together (kim_pipeline/pipeline/reporting/component_identity.py "
        "and geper/report/clinical_report_builder.py) -- wording changes need "
        f"human sign-off.\n  Kim   rendered: {kim_text!r}\n  GEPER rendered: {geper_text!r}"
    )
