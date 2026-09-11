"""
README_INTEGRATION.md names each component, and uses "Bij AI" only for the
product or as part of a ruled component name.

Human's rule (2026-09-11): "The defect was never the words 'Bij AI' -- it
was a component claiming to be the whole product." The integration README
called the VCF side "Current Bij AI (VCF Interpretation Engine)" and "run
Bij AI standalone", as if GEPER alone were the product. Approved per-row
2026-09-12: the combined system stays "Bij AI Integrated Pipeline"; GEPER is
"the Bij AI variant-interpretation component (GEPER)", or "GEPER" after
first mention; Kim is "the Bij AI sequencing-analysis component (Kim)".

Lives in kim_pipeline/tests because CI runs this directory
(.github/workflows/pytest.yml, kim_pipeline-tests job); bridge/tests, the
bridge's own suite, is not run by CI.
"""

from __future__ import annotations

import re
from pathlib import Path

_README = Path(__file__).resolve().parents[2] / "README_INTEGRATION.md"
_ALLOWED_CONTINUATIONS = (
    "Integrated Pipeline",  # the product
    "variant-interpretation component",  # GEPER, ruled name
    "sequencing-analysis component",  # Kim, ruled name
)


def _text() -> str:
    return _README.read_text(encoding="utf-8")


def test_every_bij_ai_is_the_product_or_a_ruled_component_name():
    # Whitespace collapsed: a name wrapped across a line is still the name.
    flat = " ".join(_text().split())
    offenders = []
    for m in re.finditer(r"Bij AI", flat):
        following = flat[m.end() :].lstrip()
        if not following.startswith(_ALLOWED_CONTINUATIONS):
            offenders.append(flat[max(0, m.start() - 30) : m.end() + 40])
    assert offenders == []


def test_no_bare_component_label_survives():
    text = _text()
    for label in (
        "Current Bij AI",
        "current Bij AI",
        "run Bij AI",
        "Run Bij AI",
        "Bij AI's own",
        "(Bij AI)",
    ):
        assert label not in text, label


def test_title_is_the_approved_heading():
    first = _text().splitlines()[0]
    assert first == (
        "# Bij AI Integrated Pipeline — sequencing-analysis component (Kim) "
        "+ variant-interpretation component (GEPER)"
    )
