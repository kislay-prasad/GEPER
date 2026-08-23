"""
tests/test_cmd_serve_target.py
────────────────────────────────
Regression coverage for the `serve` subcommand's target: `main.py`'s
`cmd_serve` used to check for `geper/api/app.py` (a `create_app()`
factory) which has never existed in this tree, printing "not yet
implemented" and exiting 1 -- the only documented API-launch path, and
it never worked. No test exercised `cmd_serve` at all before this file,
which is exactly why the defect was invisible to every mechanism that
watches for change: there was no prior working state to regress from.

Asserts on the actual target `uvicorn.run` is given, not just the
return code -- a test that only checked "did it return 0" would pass
for the wrong reason if some other file happened to satisfy a stale
existence check.
"""

import argparse
from pathlib import Path
from unittest import mock

from main import cmd_serve


def _serve_args(**overrides) -> argparse.Namespace:
    defaults = dict(host="127.0.0.1", port=8000, reload=False, log_level="INFO")
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_target_module_exists_and_defines_app():
    """The module cmd_serve's existence check looks for must actually be
    there, and must actually define the ASGI app object it's launched
    as -- both sides of the check, not just the file's presence."""
    api_main = Path(__file__).resolve().parent.parent / "api" / "main.py"
    assert api_main.exists(), f"cmd_serve's target does not exist: {api_main}"

    import importlib

    module = importlib.import_module("api.main")
    assert hasattr(module, "app"), "api/main.py must define a module-level `app` FastAPI instance"


@mock.patch("uvicorn.run")
def test_cmd_serve_launches_the_real_target(mock_run):
    # Patched at "uvicorn.run" (the real package), not "main.uvicorn.run":
    # cmd_serve() does `import uvicorn` locally inside the function body,
    # so `main` never has a module-level `uvicorn` attribute for
    # mock.patch to navigate to -- the local import binds to whatever
    # object sys.modules['uvicorn'] already is, which is exactly what
    # patching the real module's own `.run` attribute intercepts.
    """THE DANGEROUS CASE: cmd_serve must launch 'api.main:app' -- not
    the old phantom 'geper.api.app:create_app', and not `factory=True`
    (api/main.py exposes a plain `app` instance, not a factory
    function)."""
    result = cmd_serve(_serve_args())

    assert result == 0
    mock_run.assert_called_once()
    target = mock_run.call_args[0][0]
    assert target == "api.main:app"
    assert (
        "factory" not in mock_run.call_args.kwargs
        or mock_run.call_args.kwargs["factory"] is not True
    )
