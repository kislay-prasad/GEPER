#!/usr/bin/env python3
"""Recurring check: which function/method-local third-party imports are
declared nowhere in this repository's requirements files.

WHY THIS EXISTS: `fastapi`, `psycopg`, and `bcrypt` were each found by
something breaking in production -- a lazy (function-local) import means a
missing dependency is invisible at startup and even to a readiness probe
that opens a real database connection, because the import statement itself
never runs until the function is called. `pyotp` was the fourth instance,
and the first one found by looking instead of by something breaking
(clinical/requirements.txt, declared 2026-09-08). This script is how a
fifth one gets found the same way `pyotp` was, instead of the way the
first three were.

DESIGN CONSTRAINT THIS SCRIPT IS BUILT AROUND: a check that re-derives the
whole judgement every run reproduces the wall of false positives that gets
a tool ignored (enformer_pytorch/borzoi_pytorch/huggingface_hub are
undeclared-but-fine BY DESIGN; kim_pipeline's torch/transformers and
weasyprint are undeclared-in-base-but-fine because they are optional
extras guarded by try/except ImportError; a handful of scripts are
explicitly-labelled manual dev tooling with their own install
instructions). Re-deciding all of that every run either takes real
judgement every time or produces noise. So the judgement is captured ONCE
below in BASELINE, and this script's only job on every subsequent run is
DIFFING the current scan against that baseline and reporting what's NEW.
The baseline is the asset; this script is just a diff against it.

WHAT COUNTS AS "FUNCTION-LOCAL": an `import X` / `from X import Y`
statement whose nearest enclosing scope is a function or method body (a
node reachable by walking up through FunctionDef/AsyncFunctionDef without
crossing a Module or ClassDef boundary first). Explicitly NOT included:
module-level imports, class-body-level imports, `if TYPE_CHECKING:`
imports (those never execute at runtime and can't cause a runtime
ModuleNotFoundError), and module-level `try:/except ImportError:` guards
(a different, more visible pattern -- these fail at import time, not at
first use, so a startup probe already has a chance to catch them).

STDLIB / FIRST-PARTY EXCLUSION METHOD:
  - stdlib: `sys.stdlib_module_names` (Python 3.10+), plus a short list of
    stdlib-adjacent names that aren't in that set on every build
    (`_thread`, `__future__`).
  - first-party: the top-level import name matches one of this repo's own
    package roots (geper, kim_pipeline, clinical, bridge, shared, pipeline
    -- see the two-`pipeline` note below), OR the import is a relative
    import (`from . import x`), OR it resolves to a `.py` file that
    actually exists under one of the five package roots.

TWO NAME TRAPS THIS SCRIPT NAVIGATES, NOT BY STRING MATCHING PACKAGE NAMES:
  1. TWO PACKAGES BOTH CALLED `pipeline`: geper/pipeline/ and
     kim_pipeline/pipeline/ are different packages that happen to share a
     bare name. This script treats "first-party" as "importable from
     under one of the five package roots by walking the filesystem", so a
     `from pipeline.X import Y` inside a file under kim_pipeline/ is
     resolved against kim_pipeline/pipeline/, never confused with
     geper/pipeline/, because resolution is root-relative, not
     name-relative.
  2. kim_pipeline's DECLARED DISTRIBUTION NAME has changed identity
     mid-project (was erroneously `name = "geper"`, is `name =
     "kim-pipeline"` as of commit 1ef7619) -- irrelevant here, because this
     script never matches against `[project] name`; it matches declared
     dependencies against actual `dependencies = [...]` / requirements.txt
     line contents, and against the *importable* module name (e.g.
     `pyyaml` declares, `yaml` is imported), not the distribution name.

USAGE:
    python diagnostics/check_undeclared_function_imports.py
    python diagnostics/check_undeclared_function_imports.py --show-all
        (also print every already-known/accounted-for hit, not just new ones)

EXIT CODE: 0 always (this is a report tool per its dispatch -- "report it,
do not fix it" -- so it must not fail CI just for existing, catalogued
findings; a NEW undeclared import prints loudly but does not change the
exit code, since deciding severity is a human/god judgement call, not
this script's).
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The five package roots this repo's own dispatch named as the reason scope
# is the hard part here. bridge/ and shared/ have zero function-local
# third-party imports as of the baseline scan (2026-09-08) and no
# requirements files of their own -- included anyway so a future addition
# to either is caught, not silently out of scope.
PACKAGE_ROOTS = ["geper", "kim_pipeline", "clinical", "bridge", "shared"]

# Every requirements/pyproject file that can declare a dependency anywhere
# in this tree. A dependency declared in ANY of these counts as declared,
# even if it's declared in a different root than where it's imported --
# that's a location-mismatch note, not an undeclared-anywhere defect.
DECLARATION_FILES = [
    "geper/requirements.txt",
    "kim_pipeline/requirements.txt",
    "kim_pipeline/pyproject.toml",
    "clinical/requirements.txt",
    "pyproject.toml",
]

# Import name -> PyPI/requirements-file name, for the cases where they
# differ. Extend this as new mismatches are found; an unmapped import name
# is looked up directly, which is correct for the common case where the
# two names match.
IMPORT_TO_DECLARED_NAME = {
    "yaml": "pyyaml",
    "Bio": "biopython",
    "fm": "rna-fm",
    "PIL": "pillow",
}

# A handful of names sys.stdlib_module_names does not carry on every build.
EXTRA_STDLIB = {"_thread", "__future__", "_typeshed"}


@dataclass
class Hit:
    file: str  # repo-relative, forward slashes
    line: int
    function: str  # enclosing def/method name, or "<unknown>" if not resolvable
    statement: str  # the import statement, reconstructed
    top_level_name: str  # e.g. "pyotp" from "import pyotp.foo" or "from pyotp import X"


@dataclass
class BaselineEntry:
    top_level_name: str
    disposition: str  # DECLARED | GUARDED_OPTIONAL | AUTO_INSTALLING_BY_DESIGN | OUT_OF_SCOPE_TOOLING
    note: str
    files: list[str] = field(default_factory=list)  # repo-relative files this was seen in, informational


# ---------------------------------------------------------------------------
# THE BASELINE. This is the judgement call, captured once (2026-09-08 sweep,
# god's dispatch god-mtt1kj65-0, cross-checked by god at source and again in
# god-mtt28qwc-88 / god-mttff6md-a2). Every subsequent run's whole job is
# diffing against this list. Update it deliberately when a hit's
# disposition changes (e.g. pyotp moves from "new" to "declared" once its
# requirements.txt line lands) -- do not silently grow it to suppress noise.
# ---------------------------------------------------------------------------
BASELINE: dict[str, BaselineEntry] = {
    e.top_level_name: e
    for e in [
        BaselineEntry("psycopg", "DECLARED", "declared in geper/requirements.txt and clinical/requirements.txt"),
        BaselineEntry("bcrypt", "DECLARED", "declared in clinical/requirements.txt (the original incident; now fixed)"),
        BaselineEntry(
            "pyotp",
            "DECLARED",
            "declared in clinical/requirements.txt as of commit 346055a (andy-declare-pyotp) -- dormant (PyOtpVerifier never instantiated in production) but declared preemptively",
        ),
        BaselineEntry("yaml", "DECLARED", "declared everywhere as pyyaml"),
        BaselineEntry("uvicorn", "DECLARED", "declared in geper/, kim_pipeline/, clinical/ requirements files"),
        BaselineEntry(
            "pytest",
            "DECLARED",
            "declared (test-tier, but function-local in kim_pipeline/main.py's own CLI test runner)",
        ),
        BaselineEntry("psutil", "DECLARED", "declared in kim_pipeline/requirements.txt"),
        BaselineEntry(
            "requests",
            "DECLARED",
            "declared; used by kim_pipeline gnomad/clingen lookups and geper provenance/model loaders",
        ),
        BaselineEntry(
            "torch",
            "GUARDED_OPTIONAL",
            "kim_pipeline/pipeline/ai/engine.py: declared only under pyproject.toml's [project.optional-dependencies] 'ai' extra, not base install; wrapped in try/except ImportError (_try_import_torch), fails soft",
        ),
        BaselineEntry(
            "transformers", "GUARDED_OPTIONAL", "same as torch above -- kim_pipeline ai engine, optional extra, guarded"
        ),
        BaselineEntry("accelerate", "DECLARED", "declared in geper/requirements.txt"),
        BaselineEntry("omegaconf", "DECLARED", "declared in geper/requirements.txt"),
        BaselineEntry("torchvision", "DECLARED", "declared in geper/requirements.txt"),
        BaselineEntry("torchaudio", "DECLARED", "declared in geper/requirements.txt"),
        BaselineEntry("tensorflow", "DECLARED", "declared in geper/requirements.txt (mmsplice's backend)"),
        BaselineEntry("numpy", "DECLARED", "declared in geper/requirements.txt"),
        BaselineEntry("Bio", "DECLARED", "declared as biopython in geper/requirements.txt"),
        BaselineEntry(
            "weasyprint",
            "GUARDED_OPTIONAL",
            "kim_pipeline/pipeline/reporting/stage.py: declared only under pyproject.toml's 'pdf' extra, not base install; guarded by except ImportError: pass; second of three PDF fallback tiers",
        ),
        BaselineEntry(
            "enformer_pytorch",
            "AUTO_INSTALLING_BY_DESIGN",
            "geper/pipeline/models/enformer_plugin.py: config-gated (ENABLE_ENFORMER), documented in geper/requirements.txt's own header as intentionally unpinned and auto-installed at runtime",
        ),
        BaselineEntry(
            "borzoi_pytorch",
            "AUTO_INSTALLING_BY_DESIGN",
            "geper/pipeline/models/borzoi_plugin.py: same as enformer_pytorch, ENABLE_BORZOI-gated",
        ),
        BaselineEntry(
            "huggingface_hub",
            "AUTO_INSTALLING_BY_DESIGN",
            "geper/models/rna_fm.py: undeclared in requirements, but guarded by ensure_pip_package_available() which auto-installs via pip at runtime and falls through gracefully (returns None) if that also fails -- a live network-reach path, tracked separately under the Part 3 offline-cache report, not a ModuleNotFoundError defect",
        ),
        BaselineEntry(
            "multimolecule",
            "OUT_OF_SCOPE_TOOLING",
            "geper/scripts/compare_rna_fm_embeddings.py: standalone, manual, not-run-by-CI comparison script whose own docstring instructs 'pip install multimolecule rna-fm' by hand first",
        ),
        BaselineEntry(
            "fm",
            "OUT_OF_SCOPE_TOOLING",
            "geper/kaggle_bp7_verification/bp7_kaggle_verification.py: that subtree's own requirements.txt comments rna-fm as optional/Kaggle-notebook-inline-install; also declared as rna-fm mapping above for the general case",
        ),
        # --- Added on the first recurring run against master 1ef7619
        # (2026-09-09, god-mttff6md-a2). Everything below is either
        # correctly-declared test-tier third-party (found now only because
        # the original 2026-09-08 sweep stated test files were clean
        # without naming every declared package by name) or a second
        # instance of an already-known disposition class. None of these
        # are new defects; recorded so the next run doesn't re-flag them.
        BaselineEntry("PIL", "DECLARED", "declared as pillow; geper/tests/test_summary_pdf_logo.py"),
        BaselineEntry(
            "anyio",
            "DECLARED",
            "declared in clinical/requirements.txt (starlette TestClient dependency); kim_pipeline/tests/test_gap_fixes.py",
        ),
        BaselineEntry(
            "evo2", "DECLARED", "declared in geper/requirements.txt; geper/models/evo2.py's own lazy model-load import"
        ),
        BaselineEntry(
            "fastapi",
            "DECLARED",
            "declared in geper/requirements.txt and clinical/requirements.txt; test-file TestClient imports across geper/kim_pipeline/clinical",
        ),
        BaselineEntry(
            "httpx", "DECLARED", "declared in clinical/requirements.txt; kim_pipeline/tests/test_fixes_6_to_11.py"
        ),
        BaselineEntry(
            "pypdf",
            "DECLARED",
            "declared in kim_pipeline/pyproject.toml's pdf extra / requirements; PDF-content-assertion tests across kim_pipeline/tests",
        ),
        BaselineEntry(
            "reportlab",
            "DECLARED",
            "declared (base install, primary PDF backend); tests assert against its own output structure",
        ),
        BaselineEntry(
            "setuptools",
            "DECLARED",
            "declared; kim_pipeline/tests/test_gap_fixes.py checks it's importable as part of a packaging-hygiene test",
        ),
        BaselineEntry(
            "standalone_hyenadna",
            "AUTO_INSTALLING_BY_DESIGN",
            "geper/models/hyenadna.py: NOT a PyPI package -- git-cloned to disk and sys.path-inserted at runtime by this module's own bootstrap (importlib.util.find_spec gate, auto-clone-if-missing, documented in-file). 'declared nowhere in requirements.txt' is correct and expected, same shape as enformer_pytorch/borzoi_pytorch",
        ),
        BaselineEntry(
            "tomli",
            "GUARDED_OPTIONAL",
            "kim_pipeline/tests/test_offline_mode_and_vep_wiring.py: nested try/except -- tomllib (stdlib 3.11+) first, tomli second, raw-text regex scan as final fallback if both are absent; never crashes",
        ),
        BaselineEntry(
            "markdown_it",
            "GUARDED_OPTIONAL",
            "geper/tests/test_disclaimer_consistency.py only, not used in any production path; wrapped in try/except ImportError -> self.skipTest(...)",
        ),
    ]
}

TYPE_CHECKING_NAMES = {"TYPE_CHECKING"}


def is_stdlib(name: str) -> bool:
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    return name in stdlib or name in EXTRA_STDLIB


def package_roots_on_disk() -> dict[str, Path]:
    return {root: REPO_ROOT / root for root in PACKAGE_ROOTS if (REPO_ROOT / root).is_dir()}


def is_first_party(name: str, roots: dict[str, Path]) -> bool:
    if name in roots:
        return True
    # A first-party import name resolves under one of the roots as a
    # subpackage/module even when the top-level name looks generic (e.g.
    # `pipeline`, which exists as a subpackage of BOTH geper/ and
    # kim_pipeline/ -- either match is first-party, so this is
    # deliberately root-agnostic rather than trying to disambiguate which
    # `pipeline` a given import means).
    for root_path in roots.values():
        if (root_path / name).is_dir() or (root_path / f"{name}.py").is_file():
            return True
    # A same-named .py file existing ANYWHERE under a package root, not just
    # at its top level, is first-party too -- this catches the sys.path-
    # insertion-at-test-time pattern (e.g. a test's setUpClass adding its
    # own fixtures/ directory to sys.path before importing a same-named
    # local helper module). A third-party PyPI package is vanishingly
    # unlikely to share its exact module name with a file already vendored
    # in this tree, so this is a deliberate, documented tradeoff toward
    # fewer false positives over catching a name collision that has never
    # happened here.
    for root_path in roots.values():
        if next(root_path.rglob(f"{name}.py"), None) is not None:
            return True
    return False


def enclosing_function_name(node_stack: list[ast.AST]) -> str | None:
    """Walk the ancestor stack (outermost..innermost, excluding the import
    node itself) and return the nearest enclosing function/method name, or
    None if the nearest scope-defining ancestor is Module or ClassDef
    (i.e. this import is NOT function/method-local)."""
    for ancestor in reversed(node_stack):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ancestor.name
        if isinstance(ancestor, (ast.Module, ast.ClassDef)):
            return None
    return None


def inside_type_checking_block(node_stack: list[ast.AST]) -> bool:
    for ancestor in node_stack:
        if isinstance(ancestor, ast.If):
            test = ancestor.test
            if isinstance(test, ast.Name) and test.id in TYPE_CHECKING_NAMES:
                return True
            if isinstance(test, ast.Attribute) and test.attr in TYPE_CHECKING_NAMES:
                return True
    return False


def scan_file(path: Path) -> list[Hit]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    hits: list[Hit] = []
    rel = path.relative_to(REPO_ROOT).as_posix()

    def walk(node: ast.AST, stack: list[ast.AST]) -> None:
        new_stack = stack + [node]
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                func = enclosing_function_name(new_stack)
                if func is None:
                    continue
                if inside_type_checking_block(new_stack):
                    continue
                if isinstance(child, ast.ImportFrom):
                    if child.level and child.level > 0:
                        continue  # relative import, always first-party
                    if child.module is None:
                        continue
                    top = child.module.split(".")[0]
                    names = ", ".join(a.name for a in child.names)
                    stmt = f"from {child.module} import {names}"
                else:
                    for alias in child.names:
                        top = alias.name.split(".")[0]
                        stmt = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                        hits.append(Hit(rel, child.lineno, func, stmt, top))
                    continue
                hits.append(Hit(rel, child.lineno, func, stmt, top))
            else:
                walk(child, new_stack)

    walk(tree, [])
    return hits


def load_declared_names() -> set[str]:
    declared: set[str] = set()
    for rel in DECLARATION_FILES:
        f = REPO_ROOT / rel
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if f.suffix == ".toml":
                # Crude but sufficient: pull quoted tokens off dependency-shaped lines.
                if '"' not in line and "'" not in line:
                    continue
                for quote in ('"', "'"):
                    if quote in line:
                        for chunk in line.split(quote)[1::2]:
                            pkg = chunk.split("[")[0].split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
                            if pkg:
                                declared.add(pkg.lower())
            else:
                pkg = line.split("[")[0].split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
                if pkg:
                    declared.add(pkg.lower())
    return declared


def normalize_for_lookup(top_level_name: str) -> str:
    return IMPORT_TO_DECLARED_NAME.get(top_level_name, top_level_name).lower()


def main() -> int:
    show_all = "--show-all" in sys.argv

    roots = package_roots_on_disk()
    declared = load_declared_names()

    all_hits: list[Hit] = []
    py_files = []
    for root in roots.values():
        py_files.extend(root.rglob("*.py"))

    for f in py_files:
        all_hits.extend(scan_file(f))

    third_party_hits: dict[str, list[Hit]] = {}
    for h in all_hits:
        if is_stdlib(h.top_level_name):
            continue
        if is_first_party(h.top_level_name, roots):
            continue
        third_party_hits.setdefault(h.top_level_name, []).append(h)

    new_names = sorted(set(third_party_hits) - set(BASELINE))
    known_names = sorted(set(third_party_hits) & set(BASELINE))

    # A baseline entry marked DECLARED is a claim about TODAY's requirements
    # files, not a permanent fact -- if the line is ever removed (a revert,
    # a merge conflict resolved the wrong way, a requirements file
    # rewritten), the package is undeclared again and this is exactly the
    # bcrypt/pyotp shape recurring silently. So DECLARED baseline entries
    # are re-verified against the live `declared` set every run, not just
    # trusted from their static note.
    regressions = [
        name
        for name in known_names
        if BASELINE[name].disposition == "DECLARED" and normalize_for_lookup(name) not in declared
    ]

    print(f"Scanned {len(py_files)} files under {', '.join(PACKAGE_ROOTS)}.")
    print(
        f"Found {sum(len(v) for v in third_party_hits.values())} function-local third-party import "
        f"site(s) across {len(third_party_hits)} distinct package(s).\n"
    )

    if regressions:
        print("=" * 78)
        print("REGRESSION -- baseline says DECLARED, but it is NOT declared today. Report, do not fix.")
        print("=" * 78)
        for name in regressions:
            print(f"\n  {name}  (baseline note: {BASELINE[name].note})")
            for h in third_party_hits[name]:
                print(f"    {h.file}:{h.line}  in {h.function}()  -- {h.statement}")
        print()

    if new_names:
        print("=" * 78)
        print("NEW -- not in baseline, needs a disposition. REPORT, do not fix.")
        print("=" * 78)
        for name in new_names:
            declared_as = normalize_for_lookup(name)
            is_declared = declared_as in declared
            print(f"\n  {name}  ({'declared as ' + declared_as if is_declared else 'DECLARED NOWHERE'})")
            for h in third_party_hits[name]:
                print(f"    {h.file}:{h.line}  in {h.function}()  -- {h.statement}")
    elif not regressions:
        print(
            "No new function-local third-party imports outside the baseline, "
            "and no DECLARED baseline entry has regressed."
        )

    if show_all:
        print("\n" + "=" * 78)
        print("BASELINE (already accounted for)")
        print("=" * 78)
        for name in known_names:
            entry = BASELINE[name]
            print(f"\n  {name}  [{entry.disposition}]  {entry.note}")
            for h in third_party_hits[name]:
                print(f"    {h.file}:{h.line}  in {h.function}()")

    stale = sorted(set(BASELINE) - set(third_party_hits))
    if stale and show_all:
        print("\n" + "=" * 78)
        print("BASELINE ENTRIES NOT SEEN IN THIS SCAN (removed, or moved out of a function -- informational only)")
        print("=" * 78)
        for name in stale:
            print(f"  {name}  [{BASELINE[name].disposition}]")

    # Always exit 0: this is a report tool, not a gate. Severity is a
    # human/god call per the dispatch that created it.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
