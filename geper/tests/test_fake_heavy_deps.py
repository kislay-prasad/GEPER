"""
Tests for _fake_heavy_deps.py's activation signal.

[[conftest-silently-substitutes-fake-heavy-deps-with-no-signal]]: this
module can silently swap fake torch/transformers stubs into the entire
geper test session (conftest.py calls install() before any test module
collects) with zero print/log/warning when a stub actually activates --
a stub-backed run and a real-dependency run were, before this fix,
indistinguishable from their output alone. These tests assert that a
stub activation is now visibly logged.
"""

import logging
import sys

import _fake_heavy_deps


def _pop_module_tree(prefix: str) -> dict:
    """Remove `prefix` and `prefix.*` from sys.modules, returning what
    was removed so the caller can restore it afterward. Needed because
    `_install_*_stub` mutates sys.modules directly and this process's
    real torch/transformers are genuinely installed -- a test that
    forces the stub path must not leak a fake module into any test
    that runs after it."""
    removed = {}
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            removed[name] = sys.modules.pop(name)
    return removed


def _restore_module_tree(removed: dict) -> None:
    for name, mod in removed.items():
        sys.modules[name] = mod


def test_torch_stub_activation_is_logged(caplog):
    """Forcing `import torch` to fail (sys.modules["torch"] = None is
    the standard way to simulate an unimportable package) and then
    calling _install_torch_stub() must produce a log line saying so --
    before this fix, this path was completely silent."""
    saved = _pop_module_tree("torch")
    sys.modules["torch"] = None  # makes `import torch` raise ImportError
    try:
        with caplog.at_level(logging.WARNING):
            _fake_heavy_deps._install_torch_stub()
        assert any("torch" in rec.message.lower() and "stub" in rec.message.lower() for rec in caplog.records), (
            f"expected a stub-activation warning, got: {[r.message for r in caplog.records]}"
        )
    finally:
        _pop_module_tree("torch")
        _restore_module_tree(saved)


def test_transformers_stub_activation_is_logged(caplog):
    """Same as above, for the transformers stub."""
    saved = _pop_module_tree("transformers")
    sys.modules["transformers"] = None
    try:
        with caplog.at_level(logging.WARNING):
            _fake_heavy_deps._install_transformers_stub()
        assert any("transformers" in rec.message.lower() and "stub" in rec.message.lower() for rec in caplog.records), (
            f"expected a stub-activation warning, got: {[r.message for r in caplog.records]}"
        )
    finally:
        _pop_module_tree("transformers")
        _restore_module_tree(saved)


def test_no_log_when_real_torch_is_already_importable(caplog):
    """The common case on this floor's verified environments: real torch
    IS importable, so _install_torch_stub() must be a silent no-op (no
    stub-activation warning) -- this test would fail if the fix were
    implemented as an unconditional log instead of one gated on the
    stub actually being installed."""
    assert "torch" in sys.modules or True  # real torch may or may not be pre-imported; either is fine
    with caplog.at_level(logging.WARNING):
        _fake_heavy_deps._install_torch_stub()
    assert not any("stub" in rec.message.lower() for rec in caplog.records), (
        f"expected no stub-activation warning when real torch is available, got: {[r.message for r in caplog.records]}"
    )
