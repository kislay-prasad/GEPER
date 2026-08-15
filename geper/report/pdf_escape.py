"""
Shared XML-escaping for ReportLab `Paragraph`/`Flowable` text (round 25).

`report/summary.py` and `report/summary_short.py` build every piece of
PDF body text as a Python string fed straight to ReportLab's
`Paragraph`, which parses that string as a small XML dialect (its own
"mini-HTML": `<b>`, `<i>`, `<br/>`, `<font>`, `&nbsp;`, ...) rather than
rendering it literally -- see `reportlab.platypus.paraparser`. Neither
module has ever escaped a single interpolated value before this round;
rounds 20-24 fixed five instances of the resulting leak class one at a
time (raw exception/URL text reaching a report), each found only after
it had already reached a real report. Round 24 confirmed, by direct
reproduction against the installed `reportlab` package, exactly what an
unescaped `&`/`<`/`>` does once it reaches `Paragraph`:

  - `"R&D"` silently mangles to `"R&D;"` (ReportLab's parser treats any
    unescaped `&word` as an unterminated entity reference and silently
    closes it).
  - `"<this>"` is silently DELETED WHOLESALE -- no error, no warning,
    the bracketed text simply vanishes from the rendered page.
  - `"<br>"` (colliding with one of ReportLab's own recognized tag
    names) raises `ValueError` and crashes report generation entirely
    for that variant, for every report format being built in that call.

Round 25's audit (see ROUND_CANDIDATES.md) found this reachable from
patient-metadata fields, clinician CLI arguments (override reason/
classification/id, `approve`'s clinician-name/reg-number/hospital),
third-party `--qc-metrics-json` reason strings, and free text several
ACMG/AMP rules in `pipeline/acmg_rules.py` copy verbatim from ClinVar
(review status, clinical significance, condition/disease names) into
`rationale`/`supporting_evidence` -- none of it under this codebase's
own control, all of it currently unescaped before reaching `Paragraph`.

`esc()` below is the fix: escape `&`/`<`/`>` in a value BEFORE
interpolating it into an f-string that also contains this codebase's
own deliberate markup (`<b>`, `<br/>`, `&nbsp;`, ...) -- never escape
the composed f-string as a whole, or GEPER's own intentional formatting
would be escaped into visible `&lt;b&gt;` text alongside whatever
external value triggered the fix in the first place.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape as _xml_escape


def esc(value: Any) -> str:
    """
    Escapes `&`, `<`, `>` in `value` for safe interpolation into a
    ReportLab `Paragraph` string. `None` becomes `""` (matching the
    `... or ""`/`... or "Not provided"` fallback convention already
    used throughout `report/summary.py`/`report/summary_short.py`,
    rather than rendering the literal text "None").

    Apply this to each externally- or user-sourced value at its own
    interpolation point -- e.g. `f"<b>{esc(gene)}</b>"`, never
    `esc(f"<b>{gene}</b>")` -- so GEPER's own literal `<b>`/`<br/>`/
    `&nbsp;` markup in the surrounding template is left untouched.
    """
    if value is None:
        return ""
    return _xml_escape(str(value))
