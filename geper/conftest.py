"""
Test-session bootstrap for running GEPER's suite in an environment
without the real multi-GB torch/tensorflow/transformers stack
installed (see _fake_heavy_deps.py's own docstring: it exists
specifically so GEPER's real code can be imported/exercised end-to-end
without those installs). Installs the fake stubs, if needed, before
any test module is collected -- a no-op wherever the real packages ARE
installed. Not part of GEPER itself; safe to remove in any environment
with the real dependencies present.
"""

import os
import sys
from pathlib import Path

import _fake_heavy_deps

_fake_heavy_deps.install()

# Ensure the repo root is on sys.path so `shared/` resolves to THIS
# worktree's copy, not a different one.
#
# The editable install of the root `geper-platform` package (`pip install
# -e .`, run against the DEFAULT `python`/`py` interpreter -- see the
# floor's standing order against that) registers a MetaPathFinder via
# `sys.meta_path.append(...)` (`__editable___geper_platform_1_0_0_finder.py`,
# outside this repo, in that interpreter's site-packages) whose `MAPPING`
# hard-codes `shared` to one fixed absolute path: the primary/shared
# working tree (`C:\Users\kisla\GEPER\shared`). Because it is *appended*,
# it is a last-resort finder -- it only answers when nothing earlier in
# `sys.meta_path` (including ordinary sys.path-based resolution) has
# already found `shared`. Running pytest from `geper/` with nothing else
# on `sys.path` means nothing answers first, so that finder does: a test
# run from inside THIS worktree's `geper/` directory silently imports the
# SHARED TREE'S `shared/`, not this worktree's own, with no error --
# reproduced directly, `pytest` from `geper/` on the default interpreter
# resolves `shared.__file__` to `C:\Users\kisla\GEPER\shared\__init__.py`
# even when run from a worktree that has its own, possibly different,
# `shared/`.
#
# Inserting the repo root here first makes ordinary path-based resolution
# find THIS worktree's `shared/` before the appended finder is ever
# consulted -- the same mechanism `kim_pipeline/conftest.py` already
# provides for kim_pipeline/'s own tests (that one was incidental, not
# written for this reason; this one is deliberate).
#
# DO NOT REMOVE THIS AS UNEXPLAINED CRUFT. Removing it does not error --
# it silently restores the hazard above, which is exactly what makes it
# worth this comment instead of a bare `sys.path.insert`.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# FIX #3: api/main.py now refuses to import (sys.exit(1)) if
# GEPER_API_KEYS/GEPER_CORS_ORIGINS are both unset and GEPER_DEV_INSECURE
# isn't set -- test modules import `api.main` at MODULE level, before any
# fixture runs, so this has to be a module-level env write here, not an
# autouse fixture. conftest.py is imported before the test modules in this
# directory are collected, ensuring a module-level import can see it.
# setdefault, not a plain assignment, so a real GEPER_API_KEYS/
# GEPER_CORS_ORIGINS/GEPER_DEV_INSECURE already in the environment (e.g. a
# developer intentionally testing the auth-enabled path) is not clobbered.
os.environ.setdefault("GEPER_DEV_INSECURE", "1")
