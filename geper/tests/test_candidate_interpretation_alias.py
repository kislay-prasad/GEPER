"""
The `clinical_report` -> `candidate_interpretation` alias contract.

`candidate_interpretation_of()` is the only sanctioned way to read a
variant's candidate-interpretation object while both keys exist. Its
guarantee -- accept either key, prefer the new one -- was asserted in its
docstring and never executed, which is the same defect the recommendation
gate carried this morning: a property claimed in prose is a claim, not a
check.

WHY THIS FILE EXISTS SEPARATELY FROM THE FIXTURE SUITE.

CORRECTED 2026-08-22, because the paragraph that justified this file
described a world that has since ended. It used to read: "Eighteen test
files build documents under the deprecated `clinical_report` key and none
build the new one... migrating them to the new key WOULD silently drop
fallback coverage to zero." That was true when written. It is no longer:
the eighteen have been migrated, and every one of them now builds the new
key. The warning was in the future tense and the future has arrived.

THE POST-MIGRATION REALITY, WHICH IS THE REASON THE FILE MATTERS MORE NOW
THAN WHEN IT WAS WRITTEN: this file is the ONLY thing in `geper/tests/`
that still builds a document under the deprecated key, so it is the ONLY
thing exercising the fallback branch. The decoupling this file provides
is no longer precautionary. IT IS LOAD-BEARING.

*** THE TIDY-HAZARD, STATED HERE RATHER THAN LEFT TO BE REDISCOVERED: IF
ANYONE MIGRATES THIS FILE'S OLD-KEY CASE TO MATCH THE OTHER EIGHTEEN,
FALLBACK COVERAGE GOES TO ZERO -- SILENTLY, BECAUSE EVERY TEST STILL
PASSES. *** Nothing would go red. The branch that stops being tested is
the one an external LIMS consumer relies on until the alias is removed
(target 2026-11-22), and it would stay untested until then. The old-key
case in this file is deliberately NOT consistent with the rest of the
suite, and that inconsistency is the coverage.

The decoupling still cuts both ways, which is unchanged: the fixture
files could be migrated, or left alone, without either branch going dark.
On removal day this file remains the single thing to delete, instead of
an archaeology exercise across eighteen others.

RETIRED 2026-08-22: two `test_KNOWN_LIMIT_*` cases used to live here,
pinning the accessor's old truthiness-based fallback -- an empty
`candidate_interpretation` resolved to the deprecated key's stale copy.
The accessor is now presence-based, so both cases went red and were
DELETED rather than inverted.

THE CONVENTION, STATED BECAUSE THE NAMING DID NOT CARRY IT: a
`test_KNOWN_LIMIT_*` test asserts behaviour that is OPEN, not behaviour
that is correct. WHEN ONE GOES RED, RETIRE IT -- DELETE IT. DO NOT FLIP
THE ASSERTION TO MATCH THE NEW BEHAVIOUR. Inverting one silently
converts a record of a known defect into a claim that the defect was
never there, which is worse than having no record at all. Any
KNOWN_LIMIT test written here must say which of the two it expects.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report.clinical_report_builder import candidate_interpretation_of


class TestCandidateInterpretationAlias(unittest.TestCase):
    """
    Every case uses objects that are distinguishable from one another.
    A test in which both keys carry equal values cannot tell "preferred
    the new key" from "fell back to the old one" -- it would pass in
    both worlds, which is no test at all.
    """

    def test_deprecated_key_alone_still_resolves(self):
        # The branch external consumers depend on until 2026-11-22, and
        # the branch all eighteen fixture files happen to cover.
        old = {"marker": "deprecated-key-object"}
        self.assertIs(candidate_interpretation_of({"clinical_report": old}), old)

    def test_new_key_alone_resolves(self):
        # Nothing in the fixture suite covers this today.
        new = {"marker": "new-key-object"}
        self.assertIs(candidate_interpretation_of({"candidate_interpretation": new}), new)

    def test_both_present_prefers_the_new_key(self):
        # The case that matters after `override()`: `review/signoff.py`
        # re-points both keys at the same mutated object, but a document
        # written by an older build -- or edited by hand -- can carry a
        # stale copy under the deprecated key. The reader must not take
        # it.
        new = {"marker": "new-key-object"}
        stale = {"marker": "stale-deprecated-copy"}
        resolved = candidate_interpretation_of({"candidate_interpretation": new, "clinical_report": stale})
        self.assertIs(resolved, new)
        self.assertNotEqual(resolved["marker"], "stale-deprecated-copy")

    def test_neither_key_present_returns_none(self):
        self.assertIsNone(candidate_interpretation_of({"variant": {}}))

    def test_non_dict_input_returns_none(self):
        # The isinstance guard at the top of `candidate_interpretation_of`
        # -- a caller passing `None`/a list/a stray string must not raise.
        self.assertIsNone(candidate_interpretation_of(None))
        self.assertIsNone(candidate_interpretation_of([]))
        self.assertIsNone(candidate_interpretation_of("not a dict"))

    def test_present_but_empty_new_key_resolves_to_the_empty_object_not_none(self):
        """
        The specific behaviour the presence-based rewrite exists to
        establish (2026-08-22): "the interpretation is empty" and "there
        is no interpretation under this key" are different facts. An
        empty `candidate_interpretation` is PRESENT -- it must resolve to
        that same empty object, not be coerced to `None` the way a
        genuinely absent key is. Truthiness-based `or` could not make
        this distinction (`{}` is falsy); presence (`in`) can.
        """
        empty = {}
        resolved = candidate_interpretation_of({"candidate_interpretation": empty})
        self.assertIs(resolved, empty)
        self.assertIsNotNone(resolved)

    def test_present_but_empty_new_key_does_not_fall_through_to_a_stale_old_key(self):
        """
        Regression pin for the exact defect the two now-retired
        `test_KNOWN_LIMIT_*` cases recorded: under the old
        truthiness-based `get(new) or get(old)`, an empty-but-present new
        key was indistinguishable from an absent one, so a document
        carrying an empty `candidate_interpretation` beside a non-empty,
        stale `clinical_report` resolved to the STALE copy -- rendering
        old clinical content as current. Presence-based resolution must
        return the empty new object and never touch the stale old one.
        """
        empty_current = {}
        stale = {"marker": "stale-deprecated-copy"}
        resolved = candidate_interpretation_of({"candidate_interpretation": empty_current, "clinical_report": stale})
        self.assertIs(resolved, empty_current)
        self.assertEqual(resolved, {})


if __name__ == "__main__":
    unittest.main()
