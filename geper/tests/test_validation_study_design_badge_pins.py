"""
tests/test_validation_study_design_badge_pins.py
───────────────────────────────────────────────────

Human dispatch (conv-validation-badges, 2026-09-10): the second half of
the `[VERIFIED-IN-REPO]` badge audit in `VALIDATION_STUDY_DESIGN.md`.
That audit's first half (a prior commit, `fd5b132`, merged at
`36234d38`) fixed a stale PP5 row and pinned a sha to every badge; this
file is the second, automated half -- CI grep/runtime tests for the
handful of badges whose entire evidentiary weight rests on one exact
string or value, modeled on the drift-protection shape
`test_round16_not_evaluated_categories.py::
TestNotEvaluatedCategoryPartition::test_never_integrated_constant_matches_the_real_rule_set`
already uses for `_NEVER_INTEGRATED_ACMG_CODES`: import or call the
real production object and assert on its actual runtime value, never
re-implement or duplicate the value being checked.

*** THE LIMIT OF WHAT THIS FILE CATCHES, STATED HERE SO A READER OF A
GREEN RUN DOES NOT OVER-TRUST IT (god's own instruction, in the
dispatch's own words): a test below catches a VANISHED OR CHANGED
VALUE automatically and continuously. It catches NEITHER a moved-but-
still-correct line number in the document (fourteen of the prior
audit's nineteen findings) NOR a wrong characterisation sitting on an
intact, still-passing value -- which was PP5's more serious half: the
string "deprecated in the 2015 ACMG/AMP guideline update" was simply
WRONG, not merely stale, and no test of the current, CORRECT string
could ever have caught that the document's OLD wording asserted
something the code never said and could never have said. Automation
catches the cheap half. Only a reader re-checking the document's own
prose against the code catches the expensive half, and that is a
human/agent audit's job, not this file's. ***

Nothing here changes engine behaviour -- production code was read, not
modified, and every value this file asserts on is already correct as
of this dispatch's base (`c2258df10ccee86a92639ac47e7ca2f26a262b75`;
confirmed via `git rev-parse` before writing this file). Because
nothing is being fixed, there is no RED phase for these three tests
beyond declaring, before the first run, that they should all pass
immediately (they did) -- the same premise `test_sevengene_acceptance_
suite.py` and `test_interpretation_outcome.py`'s own already-landed
assertions rest on: RED-FIRST protects against a feature that does not
exist yet, not against a fact that is already true and merely
undocumented as a pinned fact.
"""

from __future__ import annotations

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.ps1_pm5.models import STAR_PRACTICE_GUIDELINE

_MINIMAL_CLEAN_VARIANT = {"chrom": "1", "pos": 12345, "ref": "A", "alt": "G"}


def _evaluate_minimal():
    """All-clean-empty `ACMGRuleEngine.evaluate()` call -- mirrors
    `test_interpretation_outcome.py`'s own `_evaluate` helper shape.
    PP5 is a hardcoded, unconditional `_not_evaluated` regardless of any
    input (see `acmg_rules.py`'s own PP5 block), so no evidence needs
    shaping to reach it; a minimal clean call is the least assumption-
    laden way to reach the same code path §2.1's table cites."""
    return ACMGRuleEngine().evaluate(
        clinvar_result={"records": []},
        dbsnp_result={"found": False},
        protein_result={},
        alphamissense_result={"skipped": True},
        mmsplice_result={"predicted": False},
        gnomad_result=None,
        conservation_result={},
        clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        interpro_result={},
        ensemble_result={},
        variant_dict=_MINIMAL_CLEAN_VARIANT,
        transcript_result={},
        clinvar_codon_result={},
        uniprot_result={},
        spliceformer_result={},
        splicebert_result={},
        hpo_result={},
        phenotype_result=None,
        functional_evidence_result={},
    )


class TestPP5ReasonStringMatchesTheDocumentedQuote(unittest.TestCase):
    """VALIDATION_STUDY_DESIGN.md §2.1's PP5 row quotes this exact
    string as the reason PP5 always reports `not_evaluated`. This is
    the badge that started the whole audit: its PREVIOUS wording quoted
    a string that had never existed and asserted something logically
    impossible (PP5 "deprecated in the 2015 ACMG/AMP guideline update"
    -- the criterion PP5 was introduced BY that same 2015 guideline).
    This test protects only the CURRENT, correct string from silently
    drifting again the same way -- it says nothing about whether the
    document's prose characterising that string remains accurate,
    which is exactly the gap a human reader closed once and must close
    again if the reason ever changes shape, not just content."""

    def test_pp5_reason_is_the_exact_documented_string(self):
        result = _evaluate_minimal()
        pp5 = result["all_criteria"]["PP5"]
        self.assertEqual(pp5["status"], "not_evaluated")
        # `rationale` carries a `"Not evaluated: "` prefix ahead of the
        # reason text the document quotes bare -- `assertIn`, not
        # `assertEqual`, matches what the row actually claims to cite.
        self.assertIn(
            "recommended against by ClinGen's SVI Working Group (Biesecker & Harrison 2018) as "
            "circular with respect to an independent ACMG/AMP evaluation; not applied.",
            pp5["rationale"],
        )


class TestBP6DeprecationCaveatMatchesTheDocumentedQuote(unittest.TestCase):
    """VALIDATION_STUDY_DESIGN.md's BP6 row quotes a fragment of
    `ACMGRuleEngine._BP6_DEPRECATION_CAVEAT` as evidence BP6 is reported
    at capped confidence and excluded from the point-based combining
    rules. Checked as substring containment, not full equality: the
    document quotes an excerpt with an ellipsis, not the whole
    constant, and asserting containment is the honest match for what
    the row actually claims to have verified."""

    def test_bp6_deprecation_caveat_contains_the_documented_fragment(self):
        self.assertIn(
            "reported at capped Low confidence and is excluded from this engine's own point-based combining rules",
            ACMGRuleEngine._BP6_DEPRECATION_CAVEAT,
        )
        self.assertIn(
            "so it cannot silently move the final classification",
            ACMGRuleEngine._BP6_DEPRECATION_CAVEAT,
        )


class TestStarPracticeGuidelineMatchesTheDocumentedValue(unittest.TestCase):
    """VALIDATION_STUDY_DESIGN.md §2.4 states ClinVar's maximum star
    rating is 4, citing `STAR_PRACTICE_GUIDELINE = 4`. The value itself
    is ClinVar's own review-status tiering (see `ps1_pm5/models.py`'s
    own module docstring), not something GEPER could reasonably change
    without a corresponding ClinVar policy change -- but the document's
    citation is still a claim about a specific name in a specific
    module, and that name could be renamed or the value edited without
    anyone updating the document."""

    def test_star_practice_guideline_is_four(self):
        self.assertEqual(STAR_PRACTICE_GUIDELINE, 4)


if __name__ == "__main__":
    unittest.main()
