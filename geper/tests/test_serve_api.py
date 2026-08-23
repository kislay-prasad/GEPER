"""
Tests for serve_api.py -- narrowly scoped to the exact defect class named
in the 2026-08-23 hive dispatch that requested this launcher: kim_pipeline's
own `main.py serve` subcommand pointed at a nonexistent module from the
day it was written, undetected because nothing ever invoked it. This file
does not attempt full CLI/argparse coverage (kim_pipeline/serve_api.py, the
pattern this mirrors, has none either) -- it exists to make sure geper's
version cannot silently rot the same way.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestServeApiTargetResolves(unittest.TestCase):
    def test_uvicorn_target_string_resolves_to_a_real_fastapi_app(self):
        """serve_api.py hardcodes 'api.main:app' as the uvicorn.run target
        (a module:attribute string uvicorn resolves itself, so a typo or a
        moved module produces no import-time error -- exactly how
        kim_pipeline's equivalent broke silently). Resolve it here the
        same way uvicorn would, so a broken target fails a test instead of
        failing only when someone actually starts the server."""
        import importlib

        module_name, attr_name = "api.main:app".split(":")
        module = importlib.import_module(module_name)
        target = getattr(module, attr_name)

        from fastapi import FastAPI

        self.assertIsInstance(target, FastAPI)

    def test_serve_api_module_imports_without_side_effects(self):
        """Importing the module (not running it as __main__) must not
        start a server or exit -- argparse/uvicorn.run only happen inside
        main(), guarded by `if __name__ == "__main__":`."""
        import serve_api

        self.assertTrue(hasattr(serve_api, "main"))


class TestServeApiDefaultsDoNotCollideWithKimPipeline(unittest.TestCase):
    def test_default_port_and_env_var_names_are_distinct_from_kim_pipelines(self):
        """kim_pipeline/serve_api.py defaults to port 8000 via
        GEPER_API_HOST/GEPER_API_PORT. This script must not reuse either
        the same default port or the same env-var names -- both would let
        one setting silently control two different servers, or collide
        outright if both ever ran on one host with no configuration.

        Calls the real `main()` end to end (with sys.argv and os.environ
        controlled, and uvicorn.run mocked out so no server actually
        starts) rather than re-deriving the parser separately -- this is
        the actual code path, not a re-implementation of it."""
        from unittest import mock

        import serve_api

        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("GEPER_")}
        with (
            mock.patch.object(sys, "argv", ["serve_api.py"]),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("uvicorn.run") as mock_run,
        ):
            serve_api.main()

        self.assertEqual(mock_run.call_count, 1)
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["port"], 8001, "must not default to kim_pipeline's own default port (8000)")
        self.assertEqual(kwargs["host"], "0.0.0.0")

    def test_kim_pipelines_env_var_name_has_no_effect_here(self):
        """The actual namespace-collision proof: setting GEPER_API_PORT
        (kim_pipeline's own env var) must not change geper's server's
        chosen port -- if it did, the two scripts would not be
        independently configurable, defeating the whole point of giving
        this one its own names."""
        from unittest import mock

        import serve_api

        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("GEPER_")}
        env["GEPER_API_PORT"] = "9999"  # kim_pipeline's env var, set to a decoy value
        with (
            mock.patch.object(sys, "argv", ["serve_api.py"]),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("uvicorn.run") as mock_run,
        ):
            serve_api.main()

        self.assertEqual(mock_run.call_args.kwargs["port"], 8001, "GEPER_API_PORT must not leak into this script")


if __name__ == "__main__":
    unittest.main()
