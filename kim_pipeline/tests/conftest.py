"""
tests/conftest.py
──────────────────
Project-wide pytest fixtures.
"""
from unittest.mock import patch

import pytest


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
