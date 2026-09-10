"""
DELIBERATELY BROKEN. DO NOT "FIX" THIS FILE.

This module contains a type error ON PURPOSE. It is a canary: the error
below must be reported by mypy every time it is asked, and
`geper/tests/test_mypy_canary.py` FAILS IF THE ERROR STOPS BEING REPORTED.

WHAT ITS SILENCE MEANS. If this file ever type-checks clean, the type
checker has been disarmed -- a `mypy.ini` that ignores these modules, an
`ignore_errors`, a `disable_error_code`, a scope that no longer covers
`pipeline.*`. Nothing else changed and nothing else would have told you:
mypy would keep printing "Success", the pre-commit hook would keep printing
"Passed", and CI would keep going green, all of them honestly, because a
checker told to ignore a module reports zero errors and is not lying.

WHY THIS EXISTS RATHER THAN A CONFIG ASSERTION. The only way to know a
check can fail is to make it fail on purpose. A test that reads mypy.ini
and asserts it contains the right settings pins the TEXT of the config,
not its EFFECT -- it would pass against a config whose settings are
correct and whose behaviour has been overridden somewhere else. So keep
something that is always failing on purpose, and assert that it still is.

WHY IT LIVES IN `geper/pipeline/`. It has to be inside the namespace the
configuration actually scopes. A canary parked outside the checked path
would keep singing while the checked path went silent -- it would pass
during exactly the failure it exists to detect.

IT IS NOT IN `mypy.ini`'s `files =` LIST, deliberately: an ordinary mypy
run must stay green. The canary test invokes mypy on this path explicitly,
with the same config file.

Nothing imports this module. It is syntactically valid Python, so its
presence breaks no import; it is excluded from ruff (see ruff.toml) and it
is not collected by pytest.
"""

from __future__ import annotations


def canary_returns_the_wrong_type() -> str:
    """
    Declared `-> str`, returns an `int`. mypy must report:

        error: Incompatible return value type (got "int", expected "str")
        [return-value]

    If that error is absent, read this module's docstring.
    """
    return 1  # type error ON PURPOSE -- see the module docstring
