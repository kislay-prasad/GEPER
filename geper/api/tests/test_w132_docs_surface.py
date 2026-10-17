"""
geper/api/tests/test_w132_docs_surface.py
──────────────────────────────────────────
w132 fix A on THIS app. The clinical service closed its anonymous
documentation surface; this app had the identical FastAPI default and gets
the identical gate, so that "are the interactive docs public?" has one answer
in this repository rather than two.

The negative row and its required twin, same shape as
clinical/tests/test_w132_docs_surface.py: a 404 is also what a broken app
returns, so the flag-on case must come back 200 and name a real route.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

_DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


@pytest.fixture()
def reloaded_app(monkeypatch):
    """
    Reload the current module so the import-time flag is re-read. Other tests
    can evict api.main from sys.modules, leaving a stale module reference;
    resolve the live module by name before each reload.
    """

    def _build(enabled: bool):
        if enabled:
            monkeypatch.setenv("GEPER_ENABLE_DOCS", "1")
        else:
            monkeypatch.delenv("GEPER_ENABLE_DOCS", raising=False)
        return importlib.reload(importlib.import_module("api.main")).app

    yield _build
    monkeypatch.delenv("GEPER_ENABLE_DOCS", raising=False)
    importlib.reload(importlib.import_module("api.main"))


class TestTheDocumentationIsNotAnonymouslyServed:
    def test_all_four_default_documentation_routes_are_unmounted(self, reloaded_app):
        client = TestClient(reloaded_app(enabled=False))
        statuses = {path: client.get(path).status_code for path in _DOC_PATHS}
        assert set(statuses.values()) == {404}, statuses

    def test_the_route_table_does_not_serve_them(self, reloaded_app):
        """Standing Rule 9: read what the app serves, not what a decorator declared."""
        served = {getattr(route, "path", None) for route in reloaded_app(enabled=False).routes}
        assert served.isdisjoint(set(_DOC_PATHS)), sorted(p for p in served if p in _DOC_PATHS)

    def test_the_submission_routes_are_not_named_in_the_anonymous_response(self, reloaded_app):
        body = TestClient(reloaded_app(enabled=False)).get("/openapi.json").text
        assert "/interpretations" not in body
        assert "/structures" not in body


class TestTheRequiredTwin:
    def test_the_flag_remounts_all_four(self, reloaded_app):
        client = TestClient(reloaded_app(enabled=True))
        statuses = {path: client.get(path).status_code for path in _DOC_PATHS}
        assert set(statuses.values()) == {200}, statuses

    def test_the_enabled_schema_still_names_the_real_routes(self, reloaded_app):
        spec = TestClient(reloaded_app(enabled=True)).get("/openapi.json").json()
        assert "/structures/{accession}" in spec["paths"]
        assert "/interpretations" in spec["paths"]


class TestNothingRealWasUnmounted:
    def test_the_structures_route_is_still_mounted_with_the_docs_off(self, reloaded_app):
        """
        An unmounted route 404s. This one is fail-open with GEPER_API_KEYS
        unset, so a mounted route answers something that is not 404 — which is
        what separates "the docs went" from "the router went".
        """
        client = TestClient(reloaded_app(enabled=False))
        assert client.get("/structures/NOT-AN-ACCESSION").status_code != 404

    def test_the_schema_still_builds_in_process_with_the_docs_off(self, reloaded_app):
        """
        The ruling: the documentation stays in the code, only the anonymous
        surface goes. `app.openapi()` is how geper/tests reads it.
        """
        assert "/structures/{accession}" in reloaded_app(enabled=False).openapi()["paths"]
