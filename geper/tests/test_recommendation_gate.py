"""
Tests for the recommendation-entry gate (A-1/A-1b).

The defect this closes was not the two bad strings. It was that
`_RECOMMENDATIONS_BY_CLASSIFICATION` had NOTHING enforcing the
distinction between "a next step the clinician takes" and "a statement
about what the evidence means" -- so two entries stated clinical
conclusions in GEPER's own voice, and the next entry anyone added would
have faced no check at all.

These tests therefore aim at the GATE, not at the two strings. A gate
that rejected those two by name would be a blacklist one level up: it
would pass every test we could write today and admit the next
conclusion-shaped entry unchallenged. So the tests below feed the gate
strings it has never seen and require it to decide them on its own
criteria.
"""

import unittest

from pipeline.interpretation_result import (
    _RECOMMENDATIONS_BY_CLASSIFICATION,
    is_clinician_action,
)


class TestGateAcceptsClinicianActions(unittest.TestCase):
    """Phrasings that instruct the clinician to do something."""

    def test_accepts_a_plain_imperative(self):
        self.assertTrue(is_clinician_action("Confirm the variant call with an orthogonal method."))

    def test_accepts_every_clause_of_a_multi_clause_action(self):
        self.assertTrue(
            is_clinician_action(
                "Correlate with patient phenotype and family history; consider genetic counseling referral."
            )
        )

    def test_accepts_a_negative_instruction(self):
        self.assertTrue(is_clinician_action("Do not use for clinical decision-making without additional evidence."))

    def test_accepts_an_imperative_the_shipped_map_does_not_use(self):
        # Never appears in the map -- the gate must accept it on its
        # criteria, not because it recognises a shipped string.
        self.assertTrue(is_clinician_action("Repeat the assay on a fresh specimen."))


class TestGateRejectsStatementsAboutEvidence(unittest.TestCase):
    """Phrasings that assert what the evidence means. None of these name a step."""

    def test_rejects_an_assertion_about_the_variant(self):
        self.assertFalse(
            is_clinician_action(
                "Population frequency and/or other evidence is inconsistent with a rare-disease-causing role for this variant."
            )
        )

    def test_rejects_an_assertion_about_what_the_evidence_favors(self):
        self.assertFalse(is_clinician_action("Evidence favors a benign interpretation."))

    def test_rejects_a_conclusion_never_shipped_in_this_codebase(self):
        # The discriminating case: a conclusion the gate has never been
        # shown. If the gate were a blacklist of the two known strings
        # this would pass and the test would fail.
        self.assertFalse(is_clinician_action("This variant is unlikely to affect protein function."))

    def test_rejects_a_mixed_entry_on_its_statement_clause(self):
        # One directive clause does not launder a conclusion clause.
        self.assertFalse(
            is_clinician_action(
                "Insufficient evidence for a definitive classification; do not use for clinical decisions."
            )
        )


class TestShippedMapSatisfiesTheGate(unittest.TestCase):
    def test_every_shipped_entry_is_a_clinician_action(self):
        for classification, entries in _RECOMMENDATIONS_BY_CLASSIFICATION.items():
            for entry in entries:
                with self.subTest(classification=classification, entry=entry):
                    self.assertTrue(
                        is_clinician_action(entry), f"{classification!r} entry is not a clinician action: {entry!r}"
                    )

    def test_no_shipped_entry_states_a_conclusion_about_the_variant(self):
        joined = " ".join(e for entries in _RECOMMENDATIONS_BY_CLASSIFICATION.values() for e in entries)
        self.assertNotIn("inconsistent with a rare-disease-causing role", joined)
        self.assertNotIn("favors a benign interpretation", joined)


class TestBenignHasNoRecommendationRatherThanAConclusion(unittest.TestCase):
    """
    The honest consequence of the gate: the Benign entry was *entirely*
    a conclusion, with no step named anywhere in it, so nothing survives
    it. An empty list is the correct outcome -- inventing replacement
    clinical guidance is not an engineering decision -- and the Markdown
    renderer already prints an explicit "no recommendations" line, so
    this is a visible empty state rather than a silent one.
    """

    def test_benign_yields_no_recommendations(self):
        self.assertEqual(_RECOMMENDATIONS_BY_CLASSIFICATION.get("Benign", []), [])


class TestClauseBoundaryPremiseDoesNotRot(unittest.TestCase):
    """
    The gate splits an entry into clauses and requires every clause to
    open with an imperative. That rests on an assumption about *how a
    conclusion gets attached to an action*, and the assumption was never
    written down: that it is attached with a semicolon or a full stop.

    It was true for the entries shipping today, because the splitter was
    written to fit them. It is not a property of English. These tests
    exist so the premise is executed rather than believed -- the two
    previous self-checks in this codebase that needed repair were both
    caught by running them, not by reading them.
    """

    def test_a_conclusion_after_a_lowercase_sentence_start_is_rejected(self):
        # The splitter required a CAPITAL after the full stop, so a
        # lowercase continuation was never treated as its own clause and
        # its content was never inspected at all.
        self.assertFalse(is_clinician_action("Discuss with the lab. the result is benign."))

    def test_the_shipped_entries_still_pass_after_that_tightening(self):
        # The guard that makes the tightening safe rather than merely
        # stricter: parenthetical abbreviations ("e.g. Sanger") must not
        # be mistaken for sentence boundaries.
        self.assertTrue(
            is_clinician_action(
                "Confirm the variant call with an orthogonal method (e.g. Sanger sequencing) before clinical reporting."
            )
        )

    def test_KNOWN_LIMIT_a_comma_joined_conclusion_still_passes(self):
        """
        *** THIS TEST PINS A HOLE THAT IS OPEN, NOT ONE THAT IS CLOSED. ***

        A comma is not a clause boundary here, so a conclusion hung off
        an imperative with a comma is never inspected:

            "Refer to a specialist, the evidence favors pathogenicity."

        It cannot be closed by adding "," to the splitter. Two entries
        shipping today carry ordinary commas -- one inside parentheses
        ("(segregation, functional studies)") and one in a plain list
        ("population, functional, or segregation data") -- and splitting
        on commas rejects both. Closing this needs the splitter to
        understand parentheses and lists, i.e. to become a parser.

        The assertion is deliberately the WRONG-LOOKING direction. If
        someone later closes the hole properly, this test fails and they
        delete it, which is exactly the notification we want. A limit
        recorded only in a docstring is a claim; a limit recorded in a
        test is checked.

        *** WHEN THIS GOES RED, RETIRE IT -- DELETE IT. DO NOT FLIP THE
        ASSERTION TO MATCH THE NEW BEHAVIOUR. *** Inverting it would
        silently turn a record of a known defect into a claim the defect
        was never there. Stated explicitly because the KNOWN_LIMIT name
        carries this intent for whoever wrote it and not for a stranger
        who finds the test red at some later date.
        """
        self.assertTrue(is_clinician_action("Refer to a specialist, the evidence favors pathogenicity."))

    def test_KNOWN_LIMIT_an_and_joined_conclusion_still_passes(self):
        # Same hole, different conjunction. Splitting on "and" is worse
        # than splitting on commas: "phenotype and family history" is a
        # single clause in a shipped entry.
        self.assertTrue(is_clinician_action("Monitor the patient and note that this variant is benign."))

    def test_an_unlisted_verb_fails_LOUDLY_which_is_the_safe_direction(self):
        # A legitimate new imperative is rejected until someone adds it
        # to the whitelist. That is the deliberate trade: the whitelist
        # rots toward a crash at import, never toward silent acceptance.
        self.assertFalse(is_clinician_action("Escalate to the ordering clinician."))


if __name__ == "__main__":
    unittest.main()
