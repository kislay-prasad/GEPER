"""
Every third-party package geper/api imports -- directly, or through an import
that is known to need one -- is DECLARED in geper/requirements.txt, rather
than arriving transitively from something else.

Why this exists (2026-09-11): pinning transformers to the shipped 4.56.2
(commit be489cd) broke CI's geper job at collection.
api/tests/test_interpretations_api.py does `from fastapi.testclient import
TestClient`, and starlette.testclient raises RuntimeError unless httpx2 or
httpx is importable. Nothing in geper/requirements.txt declared httpx. It had
only ever been installed because transformers 5.x pulled huggingface_hub 1.x,
which depends on httpx. At transformers==4.56.2 pip resolves huggingface_hub
0.36.2, which does not, so httpx was gone. CI run 34623361700 (ours) vs
34623788453 (master): master installed httpx 0.28.1 via huggingface_hub
1.31.0, and ours installed no httpx.

A static check, not a clean-venv install: it reads the import statements and
the requirements file, so it fails the same way on any machine, whatever that
machine happens to have installed.
"""

import ast
import os
import re
import sys
import unittest

_GEPER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_GEPER_ROOT)

# Import name -> the distribution that provides it. A third-party import in
# geper/api that is NOT listed here fails the test on purpose: whoever adds
# the import has to decide which distribution it needs and declare it.
_IMPORT_TO_DIST = {
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "psycopg": "psycopg",
    "pytest": "pytest",
    "uvicorn": "uvicorn",
    "starlette": "fastapi",  # starlette ships as fastapi's own dependency
}

# Imports that need a package they do not name. starlette.testclient (which
# fastapi.testclient re-exports) imports httpx2, falling back to httpx, and
# raises RuntimeError when neither is installed. Any one of the listed
# distributions satisfies the need.
_IMPLIED = {
    "fastapi.testclient": ("httpx", "httpx2"),
    "starlette.testclient": ("httpx", "httpx2"),
}


def _normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions():
    declared = set()
    with open(os.path.join(_GEPER_ROOT, "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if match:
                declared.add(_normalise(match.group(1)))
    return declared


def _local_top_level_names():
    """Packages/modules that resolve to this repository, not to PyPI: geper's
    own top level (run with geper/ on sys.path) and the repo root (installed
    by `pip install -e .`, e.g. clinical/, shared/)."""
    names = set()
    for root in (_GEPER_ROOT, _REPO_ROOT):
        for entry in os.listdir(root):
            names.add(entry[:-3] if entry.endswith(".py") else entry)
    return names


def _api_imports():
    """{module name: [files importing it]} for absolute imports under geper/api."""
    found = {}
    for root, _, files in os.walk(os.path.join(_GEPER_ROOT, "api")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    found.setdefault(module, []).append(os.path.relpath(path, _GEPER_ROOT))
    return found


class TestApiImportsAreDeclared(unittest.TestCase):
    def setUp(self):
        self.declared = _declared_distributions()
        self.local = _local_top_level_names()
        self.imports = _api_imports()

    def _third_party(self):
        for module, files in sorted(self.imports.items()):
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top in self.local:
                continue
            yield module, top, files

    def test_every_direct_third_party_import_is_declared(self):
        problems = []
        for module, top, files in self._third_party():
            dist = _IMPORT_TO_DIST.get(top)
            if dist is None:
                problems.append(
                    f"{module} ({files[0]}): not in _IMPORT_TO_DIST -- decide its distribution and declare it"
                )
            elif _normalise(dist) not in self.declared:
                problems.append(f"{module} ({files[0]}): needs {dist}, which geper/requirements.txt does not declare")
        self.assertEqual(problems, [])

    def test_packages_an_import_needs_without_naming_are_declared(self):
        problems = []
        for module, needs in _IMPLIED.items():
            if module not in self.imports:
                continue
            if not any(_normalise(dist) in self.declared for dist in needs):
                problems.append(
                    f"{module} ({self.imports[module][0]}) needs one of {needs}; geper/requirements.txt declares none"
                )
        self.assertEqual(problems, [])

    def test_the_check_sees_the_testclient_import_it_exists_for(self):
        # If this goes, the implied-dependency test above passes vacuously.
        self.assertIn("fastapi.testclient", self.imports)


if __name__ == "__main__":
    unittest.main()
