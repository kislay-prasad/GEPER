"""
tests/conftest.py
──────────────────
Project-wide pytest fixtures.
"""

import os

import pytest

# FIX #3: api/main.py now refuses to import (sys.exit(1)) if
# GEPER_API_KEYS/GEPER_CORS_ORIGINS are both unset and GEPER_DEV_INSECURE
# isn't set -- test_api.py imports `api.main` at MODULE level (`from
# api.main import app, _RUNS`), before any fixture runs, so this has to be
# a module-level env write here, not an autouse fixture: conftest.py is
# imported before the test modules in this directory are collected, a
# fixture would run too late for a module-level import to see it.
# setdefault, not a plain assignment, so a real GEPER_API_KEYS/
# GEPER_CORS_ORIGINS/GEPER_DEV_INSECURE already in the environment (e.g. a
# developer intentionally testing the auth-enabled path) is not clobbered.
os.environ.setdefault("GEPER_DEV_INSECURE", "1")


@pytest.fixture(autouse=True)
def _isolate_gnomad_disk_cache(tmp_path, monkeypatch):
    """The gnomAD disk cache (pipeline/gnomad/cache.py) defaults to
    ~/.cache/geper/gnomad/cache.sqlite3 when a test doesn't pass an
    explicit `gnomad.cache_dir`. Without this fixture, every test that
    constructs a GnomadLookup without specifying cache_dir would share
    that one real, persistent file — so a PRESENT/ABSENT result cached
    by one test could leak into an unrelated test that mocks a
    different outcome for the same chrom:pos:ref:alt, causing flaky,
    order-dependent failures.

    This redirects the *default* path only, per test, to an isolated
    tmp_path — tests that explicitly pass their own `cache_dir` (as the
    new gnomAD cache tests do, to test persistence itself) are
    unaffected either way.
    """
    import pipeline.gnomad.cache as gnomad_cache_mod

    isolated_default = str(tmp_path / "gnomad_test_cache" / "cache.sqlite3")
    monkeypatch.setattr(gnomad_cache_mod, "default_cache_path", lambda: isolated_default)
    yield
