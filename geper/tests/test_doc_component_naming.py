"""
The two repo-root specification documents name the component, not the product.

Same human rule as geper/tests/test_geper_component_naming.py (2026-09-11):
"The defect was never the words 'Bij AI' -- it was a component claiming to be
the whole product." Ruled 2026-09-13 to extend the per-line review to
GEPER_CLINICAL_PLATFORM_SPEC.md and DOCKER.md. The marketing site under
site/ is deliberately OUT of scope -- it names the product to an audience
that has no reason to know the components exist.

This file pins lines, not patterns, for the same reason the code-side guard
does: a blanket replace across these documents would produce new false
claims while looking like consistency. So:

  (1) every "Bij AI" in either document is either followed by
      "variant-interpretation component" / "sequencing-analysis component",
      or is one of the product-level lines pinned below byte for byte;
  (2) the two §1 headings carry the FULL disambiguating name, because §1.3
      calls the platform "GEPER Clinical" and a bare "GEPER" there would be
      ambiguous between the component and the platform;
  (3) the sites that identify which component acted say "GEPER" -- pinned,
      so a later edit that reintroduces the product name fails here.

The product-level leave-sites are the ratified intended-use statement
(SPEC:1144, the prose form of the string pinned in
pipeline/explainability_engine.py), the development/validation-status
disclaimers, the CDSCO device-class line, the commercial performance-spec
line, and DOCKER.md's title (that image contains Kim, GEPER and the bridge).

The two validation-scope lines (SPEC §32.3 and §34) were initially left
open and were ruled GEPER-scoped on 2026-09-13: a validation study measures
the interpretation component's classifications, and sequencing analysis has
its own validation story.
"""

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_COMPONENT_NAME = "Bij AI variant-interpretation component (GEPER)"
_SHORT_NAME = "GEPER"

# "Bij AI" naming neither component -- the whole-product claim.
_BARE = re.compile(r"Bij AI(?!\s+(?:variant-interpretation|sequencing-analysis) component)")

_SCOPED_DOCS = ("GEPER_CLINICAL_PLATFORM_SPEC.md", "DOCKER.md")

# Lines that legitimately name the whole product, pinned byte for byte.
_PRODUCT_NAME_PINS = {
    "GEPER_CLINICAL_PLATFORM_SPEC.md": (
        "production readiness, or medical safety.** Bij AI is in development, is not",
        "The set of genes and conditions a test covers determines whether Bij AI is CDSCO",
        "**If Bij AI ships with stated performance specifications, each adopting laboratory",
        "Bij AI prioritises variants and surfaces them with evidence to a qualified human",
        "That is Bij AI's exact current status.",
        "It does not claim Bij AI is validated, certified, production-ready, or safe for",
    ),
    "DOCKER.md": (
        # The image contains Kim, GEPER and the bridge: product-level is right.
        "# Bij AI — Docker",
    ),
}

# Sites that identify WHICH COMPONENT did something.
_COMPONENT_SITE_PINS = {
    "GEPER_CLINICAL_PLATFORM_SPEC.md": (
        f"### 1.1 What the {_COMPONENT_NAME} is",
        f"### 1.2 What the {_COMPONENT_NAME} is not",
        "GEPER:",
        "not a precondition on the input. GEPER will run on anything handed to it.",
        "GEPER knows nothing about patients. It receives a VCF and returns an",
        "Any feature that would require GEPER to know a patient identity, or the Platform",
        "## 10. VCF ingestion and automatic submission to GEPER",
        "are satisfied, the platform submits it to GEPER without manual trigger.",
        # §10.3 was rewritten 2026-09-13 once the service wrapper shipped; the
        # retired sentences are quoted in place, so they stay covered here.
        "**IMPLEMENTED, 2026-09-03 (Phase 5b).** This section previously read \"GEPER's",
        "to wrap GEPER in a service interface. Both were true when written and are false",
        "The service interface exists. `geper/api/main.py` serves `POST /interpretations`",
        "identifier. GEPER never learns who the patient is, which preserves the boundary",
        "in §1.4 and limits what a GEPER compromise exposes.",
        "### 11.3 What must be recorded from GEPER",
        "- The complete document, immutably, as the record of what GEPER produced",
        "sign-off are separate records referencing it. What GEPER produced and what a human",
        "- Add variants GEPER did not surface",
        "**GEPER's classification is never silently overwritten.** A disagreement is a",
        "- Submission to GEPER, with precondition results",
        "- GEPER run documents and rendered reports",
        "The platform authenticates to `kim_pipeline` and GEPER with service credentials,",
        "- A disagreement between the interpreter and GEPER is recorded, not overwritten",
        # Ruled 2026-09-13: a validation study measures the interpretation
        # component's classifications. Sequencing analysis has its own
        # validation story, and a product-wide phrasing would make the claim
        # vaguer than the evidence.
        "Nothing in this specification claims GEPER's outputs are correct. **ISO 15189",
        "would establish whether GEPER's outputs are correct.",
    ),
    "DOCKER.md": (
        f"the {_COMPONENT_NAME} (VCF → draft clinical",
        "# Run GEPER standalone against an existing VCF",
        "- GEPER's own `requirements.txt` forces Python 3.11 or 3.12 exactly",
        "GEPER's own loaders already handle \"auto-download on first use,",
        "*separate* Python environments because GEPER's heavier PyTorch/AI stack",
        "### 1. GEPER standalone (VCF already in hand)",
        "bind-mounts `./output` straight to `/app/geper/geper_output` (GEPER's",
        "`pipeline/models/`, `models/`, and `database/` in the actual GEPER",
    ),
}


def _lines(rel):
    return (_REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()


class TestScopedDocsNameTheComponent(unittest.TestCase):
    def test_every_bij_ai_is_the_component_name_or_a_pinned_product_line(self):
        offenders = []
        for rel in _SCOPED_DOCS:
            pinned = set(_PRODUCT_NAME_PINS.get(rel, ()))
            for i, line in enumerate(_lines(rel)):
                if "Bij AI" not in line or line.strip() in pinned:
                    continue
                if _BARE.search(line):
                    offenders.append(f"{rel}:{i + 1}: {line.strip()[:110]}")
        self.assertEqual(offenders, [])

    def test_the_product_name_lines_are_byte_unchanged(self):
        for rel, pins in _PRODUCT_NAME_PINS.items():
            stripped = [line.strip() for line in _lines(rel)]
            for pin in set(pins):
                with self.subTest(rel=rel, pin=pin[:60]):
                    self.assertEqual(stripped.count(pin), pins.count(pin))

    def test_each_component_site_names_geper_byte_for_byte(self):
        for rel, pins in _COMPONENT_SITE_PINS.items():
            stripped = [line.strip() for line in _lines(rel)]
            for pin in set(pins):
                with self.subTest(rel=rel, pin=pin[:60]):
                    self.assertEqual(stripped.count(pin), pins.count(pin))

    def test_the_two_section_one_headings_carry_the_full_name(self):
        # §1.3 is "What GEPER Clinical is", so a bare "GEPER" in §1.1/§1.2
        # would be ambiguous between the component and the platform.
        stripped = [line.strip() for line in _lines("GEPER_CLINICAL_PLATFORM_SPEC.md")]
        for heading in (f"### 1.1 What the {_COMPONENT_NAME} is", f"### 1.2 What the {_COMPONENT_NAME} is not"):
            self.assertIn(heading, stripped)
        self.assertIn("### 1.3 What GEPER Clinical is", stripped)

    def test_the_marketing_site_is_out_of_scope(self):
        # Named explicitly so that widening this guard to site/ is a
        # deliberate edit here, not a silent side effect of a glob.
        self.assertEqual(set(_SCOPED_DOCS), set(_PRODUCT_NAME_PINS) | set(_COMPONENT_SITE_PINS))
        self.assertTrue((_REPO_ROOT / "site").is_dir())


class TestDocsAgreeWithTheCodeSideNames(unittest.TestCase):
    def test_the_docs_use_the_same_names_the_code_emits(self):
        import component_identity

        self.assertEqual(component_identity.COMPONENT_NAME, _COMPONENT_NAME)
        self.assertEqual(component_identity.SHORT_NAME, _SHORT_NAME)


if __name__ == "__main__":
    unittest.main()
