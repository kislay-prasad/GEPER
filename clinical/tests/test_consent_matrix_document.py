"""
clinical/tests/test_consent_matrix_document.py
──────────────────────────────────────────────
DRIFT GUARD for docs/CONSENT_ENFORCEMENT_MATRIX.md. It needs no database.

The matrix cites its evidence as `path::symbol`. This file fails when a cited file or symbol no longer exists, so the
document cannot quietly describe code that was renamed or removed. It checks that the evidence EXISTS. It does not
check that the prose is true, and it does not decide anything about consent: the document is a description of the
current implementation, not a legal or clinical policy, and this guard keeps that statement from being edited away.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "CONSENT_ENFORCEMENT_MATRIX.md"
TEST_DIRS = (REPO / "clinical" / "tests", REPO / "geper" / "api" / "tests")

# `path/to/file.py::Symbol` or `path/to/file.py::Class.method`, always inside backticks.
_REFERENCE = re.compile(r"`([A-Za-z0-9_./\-]+\.py)::([A-Za-z0-9_.]+)`")
# A bare test or test-class name inside backticks (section 3 names tests without a path).
_BARE_TEST = re.compile(r"`((?:test_|Test)[A-Za-z0-9_]+)`")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _defines(source: str, name: str) -> bool:
    pattern = rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*[:=]"
    return re.search(pattern, source, re.MULTILINE) is not None


def test_the_document_exists_and_states_what_it_is_not():
    text = _text()
    assert "Not a legal or clinical policy" in text
    assert "DPDP" in text and "NABL" in text and "production readiness" in text


def test_every_cited_file_and_symbol_exists():
    references = _REFERENCE.findall(_text())
    assert len(references) > 30, "the matrix should cite its evidence; found too few references to be meaningful"

    missing = []
    for path, symbol in references:
        file = REPO / path
        if not file.is_file():
            missing.append(f"{path} (file not found)")
            continue
        source = file.read_text(encoding="utf-8")
        for part in symbol.split("."):
            if not _defines(source, part):
                missing.append(f"{path}::{symbol} ({part!r} not defined)")
                break
    assert not missing, "the consent matrix cites evidence that no longer exists:\n" + "\n".join(sorted(set(missing)))


def test_every_bare_test_name_exists_in_a_test_file():
    names = set(_BARE_TEST.findall(_text()))
    assert names, "section 3 should name its tests"
    sources = [f.read_text(encoding="utf-8") for d in TEST_DIRS for f in d.glob("test_*.py")]
    missing = sorted(n for n in names if not any(_defines(s, n) for s in sources))
    assert not missing, f"the consent matrix names tests that do not exist: {missing}"
