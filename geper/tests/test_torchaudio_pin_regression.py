"""
Regression test for the reported Colab failure: `requirements.txt`
left `torchaudio` unpinned, so Colab's own pre-installed torchaudio
(built against a different torch release than this project's pinned
torch==2.7.1) got imported transitively and crashed with `OSError:
undefined symbol: torch_library_impl` while importing an unrelated
model (ESM2).

This test verifies two things directly, without needing a real
mismatched install to reproduce the crash:
  1. `requirements.txt` now pins `torchaudio` explicitly (so `pip
     install -r requirements.txt` always installs a known-good,
     torch==2.7.1-compatible torchaudio rather than leaving whatever
     the host environment already shipped untouched).
  2. `verify_environment.py`'s startup validator independently detects
     a torch/torchaudio version mismatch -- using fake `torch`/
     `torchaudio` modules standing in for exactly the reported Colab
     scenario (torch 2.7.1 + torchaudio 2.11.0) -- and reports it as a
     FAIL/WARN with an actionable fix, before any model is imported.
"""

import importlib.machinery
import os
import re
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestTorchaudioIsPinnedInRequirements(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_REPO_ROOT, "requirements.txt"), encoding="utf-8") as fh:
            self.requirements_text = fh.read()

    def test_torchaudio_is_pinned_exactly(self):
        match = re.search(r"^torchaudio==([\w.+-]+)\s*$", self.requirements_text, re.MULTILINE)
        self.assertIsNotNone(match, "requirements.txt must pin an exact torchaudio version")

    def test_torchaudio_pin_matches_torch_pin(self):
        """torchaudio and torch must be pinned to the SAME version -- that's the pairing PyTorch's own compatibility matrix requires for this release line."""
        torch_match = re.search(r"^torch==([\w.+-]+)\s*$", self.requirements_text, re.MULTILINE)
        torchaudio_match = re.search(r"^torchaudio==([\w.+-]+)\s*$", self.requirements_text, re.MULTILINE)
        self.assertIsNotNone(torch_match)
        self.assertIsNotNone(torchaudio_match)
        self.assertEqual(torch_match.group(1), torchaudio_match.group(1))


class TestStartupValidatorCatchesTorchaudioMismatch(unittest.TestCase):
    """
    Installs fake `torch`/`torchaudio` modules reproducing the exact
    reported scenario (torch 2.7.1, torchaudio 2.11.0+cu128 -- Colab's
    mismatched pre-installed build) and confirms
    `verify_environment.py` flags it, both via the dedicated
    `check_torchaudio()` check and via `check_dependency_compatibility()`.
    """

    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name) for name in ("torch", "torchaudio", "torchvision", "verify_environment")
        }
        for name in ("torch", "torchaudio", "torchvision", "verify_environment"):
            sys.modules.pop(name, None)

        fake_torch = types.ModuleType("torch")
        fake_torch.__version__ = "2.7.1+cu128"
        fake_torch.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        sys.modules["torch"] = fake_torch

        fake_torchaudio = types.ModuleType("torchaudio")
        fake_torchaudio.__version__ = "2.11.0+cu128"  # exactly the version from the reported Colab install
        fake_torchaudio.__spec__ = importlib.machinery.ModuleSpec("torchaudio", loader=None)
        sys.modules["torchaudio"] = fake_torchaudio

        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        import verify_environment as ve

        self.ve = ve

    def tearDown(self):
        for name, mod in self._saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_check_torchaudio_flags_the_mismatch(self):
        result = self.ve.check_torchaudio()
        self.assertEqual(result.status, "FAIL")
        self.assertIn("2.11.0", result.detail)
        self.assertIn("torch_library_impl", result.detail)
        self.assertIsNotNone(result.fix)
        self.assertIn("torchaudio==2.7.1", result.fix)

    def test_cross_package_compatibility_check_flags_the_mismatch(self):
        result = self.ve.check_dependency_compatibility()
        self.assertNotEqual(result.status, "PASS")
        self.assertIn("torchaudio==2.11.0", result.detail)
        self.assertIn("torch_library_impl", result.detail)

    def test_this_check_runs_before_any_model_import_would_happen(self):
        """
        `verify_environment.py` is a standalone script with zero
        import-time dependency on `models`/`pipeline` -- it can be run
        (and, per the README's "17. Environment verification" section,
        is intended to be run) BEFORE `main.py` ever imports ESM2 or
        any other model, which is what makes it catch this class of
        failure ahead of time rather than after the fact.
        """
        import ast

        with open(os.path.join(_REPO_ROOT, "verify_environment.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.add(node.module.split(".")[0])
        self.assertNotIn("models", top_level_imports)
        self.assertNotIn("pipeline", top_level_imports)


if __name__ == "__main__":
    unittest.main()
