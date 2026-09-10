"""
tests/test_serve_api.py
────────────────────────
Regression coverage for kim_pipeline/serve_api.py's `--host` default and its
bind-all-without-auth warning (mirrors geper/tests/test_serve_api.py, added
for the identical fix on this script).

The default was `0.0.0.0` (bind every network interface). INSTALL.md's own
documented "Development (keyless local)" recipe is `export
GEPER_DEV_INSECURE=1; python main.py serve` -- but the standalone
`serve_api.py` launcher (this file's subject, distinct from `main.py serve`,
which already defaulted to 127.0.0.1) had no such override and would bind
broadly with no override at all. "Keyless local" was local only by luck of
the network the operator happened to be on. Flipped to `127.0.0.1`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestServeApiDefaultHostIsLoopback(unittest.TestCase):
    def test_default_host_is_loopback_not_bind_all(self):
        """The actual regression proof: parse with no args and read back
        `.host` -- no port bound, no server started."""
        import serve_api

        args = serve_api.build_parser().parse_args([])
        self.assertEqual(
            args.host,
            "127.0.0.1",
            "default must be loopback-only; --host 0.0.0.0 is now an explicit choice, not a default",
        )

    def test_env_var_override_still_works(self):
        """GEPER_API_HOST must still be able to set a different default --
        the flip changes the bare default only, not the override channel."""
        from unittest import mock

        import serve_api

        with mock.patch.dict("os.environ", {"GEPER_API_HOST": "10.0.0.5"}):
            args = serve_api.build_parser().parse_args([])
        self.assertEqual(args.host, "10.0.0.5")

    def test_explicit_flag_still_overrides_to_bind_all(self):
        """`--host 0.0.0.0` must still work as an explicit, deliberate
        choice -- the fix removes the silent default, not the capability."""
        import serve_api

        args = serve_api.build_parser().parse_args(["--host", "0.0.0.0"])
        self.assertEqual(args.host, "0.0.0.0")


class TestBindAllWithoutAuthWarning(unittest.TestCase):
    """`_warn_if_bind_all_without_auth` must fire on the PAIR (bind-all AND
    auth off), never on either half alone -- see geper/tests/test_serve_api.py's
    identical class for the full reasoning. Patches the real, already-imported
    `api.main` module's `_API_KEYS` attribute directly (via
    `mock.patch.object`, not a `sys.modules` dict patch -- `import api.main
    as x` resolves through the parent package's attribute, which a
    `sys.modules["api.main"]` patch does not reach) so this exercises the
    same import path the production code takes.
    """

    @classmethod
    def setUpClass(cls):
        import importlib

        cls._api_main = importlib.import_module("api.main")

    def test_warns_when_bind_all_and_auth_off(self):
        import io
        from unittest import mock

        import serve_api

        with (
            mock.patch.object(self._api_main, "_API_KEYS", None),
            mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            serve_api._warn_if_bind_all_without_auth("0.0.0.0")
        out = fake_err.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("0.0.0.0", out)
        self.assertIn("authentication", out.lower())

    def test_no_warning_when_bind_all_but_real_keys_set(self):
        """The normal-deployment case: bind-all with real auth configured
        must stay silent -- a warning that fires here teaches people to
        ignore it."""
        import io
        from unittest import mock

        import serve_api

        with (
            mock.patch.object(self._api_main, "_API_KEYS", {"a-real-key"}),
            mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            serve_api._warn_if_bind_all_without_auth("0.0.0.0")
        self.assertEqual(fake_err.getvalue(), "")

    def test_no_warning_on_loopback_regardless_of_auth_state(self):
        """The default (127.0.0.1) must never warn, regardless of auth
        state -- and must short-circuit before even checking it."""
        import io
        from unittest import mock

        import serve_api

        with (
            mock.patch.object(self._api_main, "_API_KEYS", None),
            mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            serve_api._warn_if_bind_all_without_auth("127.0.0.1")
        self.assertEqual(fake_err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
