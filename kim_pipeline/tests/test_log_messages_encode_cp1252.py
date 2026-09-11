"""
Every string a kim_pipeline logging call formats encodes to cp1252.

Card FINDING-Kim-log-messages-still-carry-the-arrow-U+2192-...: a log message
containing U+2192 ("→") makes the logging module print "--- Logging error ---"
and a UnicodeEncodeError traceback to stderr on a Windows cp1252 console. The
run continues, but the line is lost. 6569721 fixed the --help and completion
paths, and this closes the log messages.

A source scan, not a console run: it walks every logging call in the
production tree (tests/ excluded) and encodes each string literal in its
arguments -- the format string and any literal arguments -- to cp1252.
Literals only: text that arrives at run time (a path, a VCF field) cannot be
checked statically.

OUT OF SCOPE BY RULING: the "≥" and "★" in the ACMG classifier's criteria
text. That goes to reports and JSON, not to a console, and is not passed to a
logging call.
"""

import ast
from pathlib import Path

_KIM_ROOT = Path(__file__).resolve().parents[1]
_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_logging_call(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in _LEVELS:
        return False
    target = node.func.value
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    return "log" in name.lower()  # logger, _logger, log, self.logger, logging


def _string_literals(call):
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.value


def _production_files():
    for path in sorted(_KIM_ROOT.rglob("*.py")):
        rel = path.relative_to(_KIM_ROOT)
        if rel.parts[0] in {"tests", ".venv", "venv"} or "__pycache__" in rel.parts:
            continue
        yield path, rel


def _offenders():
    found = []
    calls = 0
    for path, rel in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not _is_logging_call(node):
                continue
            calls += 1
            for text in _string_literals(node):
                try:
                    text.encode("cp1252")
                except UnicodeEncodeError as exc:
                    bad = text[exc.start : exc.end]
                    found.append(
                        f"{rel.as_posix()}:{node.lineno}: {bad!r} (U+{ord(bad[0]):04X}) in {text!r}"
                    )
    return found, calls


def test_every_kim_logging_string_encodes_to_cp1252():
    offenders, _ = _offenders()
    assert offenders == [], "\n".join(offenders)


def test_the_scan_actually_sees_logging_calls():
    # Guards against a scan that silently matches nothing and passes.
    _, calls = _offenders()
    assert calls > 100, calls
