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

import _fake_heavy_deps

_fake_heavy_deps.install()

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
