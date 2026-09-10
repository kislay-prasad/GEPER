"""
The ISO 7.4.1.6 i) element must match the STANDARD, not merely match itself.

*** THE DEFECT THIS EXISTS FOR: THE ARTEFACT CONTRADICTED ITSELF INSIDE TWO
LINES AND SHIPPED THE WRONG HALF ON FOUR SURFACES. *** `ISO_RESEARCH_ELEMENT`
said "a research AND development programme". The comment immediately below it
quoted "research OR development programme" and declared that text to be ISO's
exact wording, "not paraphrased". A reader checking the comment would have
concluded the code was right.

THE CONJUNCTION IS LOAD-BEARING, WHICH IS WHY THIS IS NOT PEDANTRY. ISO's "or"
means EITHER category qualifies -- research, or development. "and" asserts
BOTH. The entire argument for quoting the standard is that an assessor
RECOGNISES A DEFINED SLOT instead of evaluating our prose; a paraphrase
forfeits exactly that, and this one was rendered to clinicians.

VERBATIM SOURCE, ISO 15189:2022 7.4.1.6 i):
    "identification of examinations undertaken as part of a research or
     development programme and for which no specific claims on measurement
     performance are available"

WHY THE EXISTING GUARD DID NOT CATCH IT, WHICH IS THE PART WORTH REMEMBERING:
`test_disclaimer_consistency.py` DID pin this text -- in eight places -- and
every one of them pinned "research and development programme". *** THE GUARD
WAS NOT ABSENT. IT WAS ENFORCING THE PARAPHRASE. *** A pin taken from the code
it guards can only ever confirm the code has not changed; it cannot tell you
the code was right to begin with. This file pins against the STANDARD, which
is an authority outside the codebase, so it can fail when the code is wrong
rather than only when the code moves.
"""

import re
import unittest

from report.clinical_report_builder import ISO_RESEARCH_ELEMENT

#: The two fragments ISO 7.4.1.6 i) actually uses, transcribed from the clause.
_ISO_CONJUNCTION_PHRASE = "research or development programme"
_ISO_CLAIMS_PHRASE = "no specific claims on measurement performance are available"

#: The paraphrase that shipped. Named explicitly so this test fails loudly if
#: it ever comes back, rather than merely failing to find the correct one.
_PARAPHRASE = "research and development programme"


class ISOClauseIsVerbatimTest(unittest.TestCase):
    def test_the_conjunction_matches_the_standard(self):
        self.assertIn(
            _ISO_CONJUNCTION_PHRASE,
            ISO_RESEARCH_ELEMENT,
            "ISO 7.4.1.6 i) says 'research OR development programme' -- either category "
            "qualifies. 'and' asserts both, which is a different claim about this laboratory.",
        )

    def test_the_shipped_paraphrase_does_not_come_back(self):
        self.assertNotIn(
            _PARAPHRASE,
            ISO_RESEARCH_ELEMENT,
            "the 'and' paraphrase shipped on four report surfaces once already",
        )

    def test_the_measurement_performance_phrase_is_intact(self):
        """The half that was always correct. Asserted so a fix to the
        conjunction cannot quietly damage the rest of the clause."""
        self.assertIn(_ISO_CLAIMS_PHRASE, ISO_RESEARCH_ELEMENT)

    def test_the_comment_and_the_constant_do_not_contradict_each_other(self):
        """
        *** THE GENERAL FORM OF THIS DEFECT, GUARDED DIRECTLY. *** The comment
        beside the constant QUOTES the wording it claims to use. If the two
        ever disagree again, the artefact is asserting something about itself
        that is false -- and the comment is what a reviewer trusts.

        Reads the source rather than importing, because the claim under test is
        about the FILE's own text, not about a runtime value.
        """
        import report.clinical_report_builder as module

        with open(module.__file__, encoding="utf-8") as fh:
            source = fh.read()

        quoted = re.findall(r'"(research (?:and|or) development programme)"', source)
        self.assertTrue(quoted, "the comment beside ISO_RESEARCH_ELEMENT no longer quotes its own wording")
        for phrase in quoted:
            with self.subTest(quoted=phrase):
                self.assertIn(
                    phrase,
                    ISO_RESEARCH_ELEMENT,
                    f"the source quotes '{phrase}' as the wording in use, but the constant does not "
                    "contain it -- the file contradicts itself",
                )


if __name__ == "__main__":
    unittest.main()
