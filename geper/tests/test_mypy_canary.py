"""
geper/tests/test_mypy_canary.py
───────────────────────────────
THE ONLY CHECK ON THIS FLOOR THAT CATCHES A DISARMED TYPE CHECKER.

Everything else answers "did mypy run?". This answers "did mypy still
DETECT?" -- and those came apart in a measured way:

  * A commit changing only `mypy.ini` presented no file the pre-commit hook
    matched, so the hook printed "(no files to check) Skipped". The one
    commit that changes what the checker enforces was the one commit it did
    not run on. FIXED by the reworked hook.
  * With a disabling `mypy.ini` installed by that commit, a real type error
    -- `return x + 1` from a function declared `-> str` -- then committed
    with the hook printing "Passed", and `pytest` green at 2560, because the
    method is never called. THAT is what this file catches, and neither the
    hook rework nor the CI job does.

RUNNING IS NOT DETECTING. A checker told to ignore a module reports zero
errors and is not lying: mypy prints "Success", the hook prints "Passed",
CI goes green, all honestly. The green means the checker ran. It has never
meant the checker could still fail.

So `geper/pipeline/_mypy_canary.py` holds a deliberate type error, and this
test asserts THE ERROR IS STILL REPORTED. It fails when the canary goes
quiet. That inversion is the whole design: this is the one test in the
repository whose passing depends on something still being broken.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "mypy.ini"
_CANARY = _REPO_ROOT / "geper" / "pipeline" / "_mypy_canary.py"

# The exact error the canary is built to provoke. Matched on the error CODE
# rather than the prose: mypy's wording has changed between releases, its
# error codes have not.
_EXPECTED_CODE = "[return-value]"


def _run_mypy_on_the_canary() -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-color-output",
            "--config-file",
            str(_CONFIG),
            str(_CANARY),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which(sys.executable) is None, reason="no interpreter path")
class TestTheTypeCheckerCanStillFail:
    def test_the_canary_file_is_present(self):
        """
        Checked separately from the error itself. If the canary were deleted,
        mypy would report nothing, and an assertion that only looked for the
        error would fail with a message about types -- pointing at the
        checker instead of at the missing file.
        """
        assert _CANARY.exists(), (
            f"the mypy canary is missing from {_CANARY}. It is deliberately "
            "broken and must not be deleted -- see its module docstring."
        )

    def test_mypy_still_reports_the_canary_error(self):
        """
        THE ASSERTION IS THAT SOMETHING IS STILL BROKEN. If this fails, the
        type checker has been disarmed -- not fixed.
        """
        try:
            result = _run_mypy_on_the_canary()
        except FileNotFoundError:  # pragma: no cover - mypy absent locally
            pytest.skip("mypy is not installed in this environment")

        combined = result.stdout + result.stderr
        if "No module named mypy" in combined:  # pragma: no cover
            pytest.skip("mypy is not installed in this environment")

        assert _EXPECTED_CODE in combined, (
            "mypy did NOT report the canary's deliberate type error.\n\n"
            "THE TYPE CHECKER HAS BEEN DISARMED. Something in mypy.ini is now "
            "ignoring geper/pipeline/ -- an ignore_errors, a disable_error_code, "
            "or a scope that no longer covers it. mypy, the pre-commit hook and "
            "CI will all keep reporting success while checking nothing.\n\n"
            f"Expected {_EXPECTED_CODE} in mypy's output. Got exit "
            f"{result.returncode} and:\n{combined or '<no output>'}"
        )

    def test_mypy_exits_nonzero_on_the_canary(self):
        """
        Separate from the message assertion, and deliberately not folded into
        it: a run that crashed before checking anything could produce neither
        the code nor a zero exit, and the two failures want different names.
        """
        try:
            result = _run_mypy_on_the_canary()
        except FileNotFoundError:  # pragma: no cover
            pytest.skip("mypy is not installed in this environment")
        if "No module named mypy" in (result.stdout + result.stderr):  # pragma: no cover
            pytest.skip("mypy is not installed in this environment")

        assert result.returncode != 0, (
            "mypy exited 0 on a file containing a deliberate type error. "
            "See geper/pipeline/_mypy_canary.py's docstring."
        )
