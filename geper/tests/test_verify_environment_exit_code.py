"""
geper/verify_environment.py exits non-zero when its verdict is FAIL -- by
default, not only under --strict.

Human, 2026-09-11: "a check whose failure exits success is a check nothing can
act on." Until then `main()` returned 0 on `overall: FAIL` unless `--strict`
was passed, so anything gating on the exit code (a shell `&&`, a CI step, a
`docker run` whose status is checked) was told the environment was fine while
the report said it was not.

The checks themselves are replaced with fakes: what is under test is how the
verdict becomes an exit code, which must not depend on what this machine has
installed.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

import verify_environment as ve


def _check(name, status):
    def fn():
        return ve.CheckResult(name, status, f"{name} is {status}")

    fn.__name__ = name
    return fn


def _run(checks, *argv):
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(ve, "_ALL_CHECKS", checks),
        mock.patch.object(ve.sys, "argv", ["verify_environment.py", *argv]),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = ve.main()
    return code, out.getvalue(), err.getvalue()


_ONE_FAIL = [_check("a", "PASS"), _check("b", "FAIL"), _check("c", "WARN")]
_ALL_PASS = [_check("a", "PASS"), _check("b", "PASS")]
_WARN_ONLY = [_check("a", "PASS"), _check("b", "WARN"), _check("c", "SKIP")]


class TestFailExitsNonZeroByDefault(unittest.TestCase):
    def test_a_fail_environment_exits_non_zero_with_no_flags(self):
        code, out, _ = _run(_ONE_FAIL)
        self.assertIn("1 failures", out)  # the report says FAIL...
        self.assertNotEqual(code, 0)  # ...and the exit code agrees

    def test_a_fail_environment_exits_non_zero_in_json_mode(self):
        code, out, _ = _run(_ONE_FAIL, "--json")
        self.assertEqual(json.loads(out)["overall"], "FAIL")
        self.assertNotEqual(code, 0)

    def test_the_exit_code_is_one(self):
        self.assertEqual(_run(_ONE_FAIL)[0], 1)


class TestControlsStillExitZero(unittest.TestCase):
    def test_all_pass_exits_zero(self):
        code, out, _ = _run(_ALL_PASS)
        self.assertIn("0 failures", out)
        self.assertEqual(code, 0)

    def test_all_pass_exits_zero_in_json_mode(self):
        code, out, _ = _run(_ALL_PASS, "--json")
        self.assertEqual(json.loads(out)["overall"], "PASS")
        self.assertEqual(code, 0)

    def test_warnings_alone_exit_zero(self):
        # WARN means an optional feature will be skipped, not a blocking
        # failure -- the report says so, and the exit code agrees.
        self.assertEqual(_run(_WARN_ONLY)[0], 0)


class TestStrictIsRetired(unittest.TestCase):
    def test_strict_is_accepted_changes_nothing_and_says_so(self):
        for checks, expected in ((_ONE_FAIL, 1), (_ALL_PASS, 0)):
            code, _, err = _run(checks, "--strict")
            self.assertEqual(code, expected)
            self.assertIn("--strict is deprecated", err)

    def test_no_deprecation_notice_without_strict(self):
        self.assertNotIn("deprecated", _run(_ALL_PASS)[2])


if __name__ == "__main__":
    unittest.main()
