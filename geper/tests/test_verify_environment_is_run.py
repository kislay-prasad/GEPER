"""
verify_environment.py is RUN, and its FAIL verdict STOPS whatever ran it -- in
CI and in the Colab notebook.

Human, 2026-09-12: "CI yes; Colab yes with an explicit failure. A check nothing
runs is the same inert control as an exit code nobody reads. And a bare
`!python verify_environment.py` swallowing the exit code in Colab reproduces
the original defect one layer out; make the cell fail loudly."

verify_environment.py already exits 1 on FAIL (tests/
test_verify_environment_exit_code.py). These tests are about the two callers:

1. CI: the geper-tests job runs it, and nothing between the script and the
   job's status discards the exit code (no continue-on-error, no `|| true`,
   no `if:` that skips it).
2. Colab: the notebook has a verify cell, it is plain Python (a `!` shell line
   discards the exit code), and when EXECUTED against a stub script that exits
   1 it raises -- with a stub that exits 0 as the control, so a cell that
   raises unconditionally cannot pass.
"""

import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

_GEPER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _GEPER_ROOT.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pytest.yml"
_NOTEBOOK = _GEPER_ROOT / "GEPER_Colab.ipynb"

# Anything that would let a non-zero exit from the script leave the step green.
_SWALLOWERS = (
    re.compile(r"\|\|"),  # `|| true`, `|| :`, `|| echo ...`
    re.compile(r"\bset\s+\+e\b"),
    re.compile(r"\bexit\s+0\b"),
    re.compile(r";\s*(true|:)\s*$", re.MULTILINE),
    re.compile(r"\|\s*(tee|cat|head|tail)\b"),  # a pipe reports the LAST command's status
)


def _geper_job():
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["geper-tests"]


def _verify_steps(job):
    return [s for s in job["steps"] if "verify_environment" in str(s.get("run", ""))]


class TestCiRunsVerifyEnvironmentAndGatesOnIt(unittest.TestCase):
    def test_the_geper_job_has_exactly_one_verify_environment_step(self):
        self.assertEqual(len(_verify_steps(_geper_job())), 1)

    def test_the_step_invokes_the_geper_script_directly(self):
        job = _geper_job()
        (step,) = _verify_steps(job)
        # The job's default working-directory is geper/, where the script lives.
        self.assertEqual(job["defaults"]["run"]["working-directory"], "geper")
        self.assertNotIn("working-directory", step)
        self.assertRegex(step["run"].strip(), r"^python\s+verify_environment\.py(\s+--json)?$")

    def test_nothing_discards_the_exit_code(self):
        job = _geper_job()
        (step,) = _verify_steps(job)
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if", step)
        self.assertNotIn("shell", step)  # the default `sh -e` fails the step on a non-zero exit
        for pattern in _SWALLOWERS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(step["run"]))

    def test_it_runs_before_pytest(self):
        # A verdict after pytest would still gate the job, but a FAIL environment
        # should be named as such before a wall of test failures it explains.
        steps = _geper_job()["steps"]
        (step,) = _verify_steps(_geper_job())
        pytest_index = next(i for i, s in enumerate(steps) if "pytest" in str(s.get("run", "")))
        self.assertLess(steps.index(step), pytest_index)


def _cells():
    return json.loads(_NOTEBOOK.read_text(encoding="utf-8"))["cells"]


def _verify_cell_index():
    hits = [
        i
        for i, c in enumerate(_cells())
        if c["cell_type"] == "code" and "verify_environment.py" in "".join(c["source"])
    ]
    return hits


def _run_verify_cell(project_root, exit_code):
    """Execute the notebook's verify cell against a stub verify_environment.py."""
    (project_root / "verify_environment.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            print("stub verify_environment: exiting {exit_code}")
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    (index,) = _verify_cell_index()
    source = "".join(_cells()[index]["source"])
    namespace = {"PROJECT_ROOT": str(project_root), "__name__": "__main__"}
    exec(compile(source, f"GEPER_Colab.ipynb[cell {index}]", "exec"), namespace)


class TestColabVerifyCellFailsLoudly(unittest.TestCase):
    def test_the_notebook_has_exactly_one_verify_cell(self):
        self.assertEqual(len(_verify_cell_index()), 1)

    def test_the_cell_is_python_not_a_shell_escape(self):
        # `!python verify_environment.py` runs the script and throws its exit
        # code away: the report prints FAIL and the next cell runs anyway.
        (index,) = _verify_cell_index()
        for line in _cells()[index]["source"]:
            self.assertFalse(line.lstrip().startswith(("!", "%")), line)

    def test_it_runs_after_the_project_root_is_set_and_before_the_pipeline(self):
        cells = ["".join(c["source"]) for c in _cells()]
        (index,) = _verify_cell_index()
        root_cell = next(i for i, s in enumerate(cells) if "PROJECT_ROOT = " in s)
        pipeline_cell = next(i for i, s in enumerate(cells) if "GeperPipeline(" in s)
        self.assertLess(root_cell, index)
        self.assertLess(index, pipeline_cell)

    def test_a_script_that_exits_1_makes_the_cell_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception) as ctx:
                _run_verify_cell(Path(tmp), exit_code=1)
        # Loud means an exception IPython renders as an error, not SystemExit
        # (which IPython reports quietly) and not a KeyboardInterrupt.
        self.assertNotIsInstance(ctx.exception, (SystemExit, KeyboardInterrupt))
        self.assertIn("FAIL", str(ctx.exception))

    def test_control_a_script_that_exits_0_lets_the_cell_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_verify_cell(Path(tmp), exit_code=0)  # must not raise

    def test_the_cell_runs_the_script_under_the_notebook_interpreter(self):
        # Not a bare `python` from PATH, which in Colab need not be the kernel's.
        (index,) = _verify_cell_index()
        self.assertIn("sys.executable", "".join(_cells()[index]["source"]))


if __name__ == "__main__":
    sys.exit(unittest.main())
