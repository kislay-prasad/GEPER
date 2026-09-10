"""
The gnomAD "variant not found" fact is stated ONCE, and by PM2.

WHAT THIS FILE GUARDS. `InterpretationEngine.interpret()` used to seed its
"legacy" evidence list with its own sentence for a gnomAD miss ("gnomAD:
variant not found in the population database (PM2 evidence -- absent from
gnomAD).") while `ACMGRuleEngine._pm2` contributed its own, differently
worded one ("gnomAD: variant not found."). Two sentences, one fact, worded
too differently for exact-string `_dedupe` to merge -- so a reader saw it
twice (report review round 4, I8).

HOW IT WAS FIXED, AND WHY THIS FILE HAD TO CHANGE (2026-09-11, card
DEFECT-the-dead-superseded-legacy-evidence-filter). Two different repairs
were applied at two different times:

  1. `interpretation_result.py` filtered that one exact sentence out of the
     merged result (`_SUPERSEDED_LEGACY_EVIDENCE`);
  2. later, the HIGH 3 Q4-B/T3-F1 ruling removed the APPEND AT ITS SOURCE --
     `interpret()` now reads `for _text, weight in gnomad_criteria:` and
     discards the sentence entirely.

After (2), the filter from (1) could never match anything again. This file
kept it looking necessary by HAND-BUILDING `supporting_evidence: [that exact
sentence]` itself and asserting the filter removed it -- A TEST THAT SUPPLIES
AN INPUT PRODUCTION CANNOT PRODUCE, AND THEN ASSERTS THE DEAD BRANCH FIRES.
It was green forever and could not fail for any reason a reader would care
about. The filter has been deleted; these tests now drive the REAL
`interpret()` path instead of constructing the merge function's input by
hand.

THE DECISION IT ENCODES IS REAL AND IS WHY THIS WAS CONVERTED RATHER THAN
DELETED: one fact, stated once, in PM2's own words, because PM2's
criterion-level text is the more precise and current source. Nothing else in
the codebase records that; deleted, it would survive only as prose in a
review note.

AND THE PROPERTY HAS TO FAIL IF THE SENTENCE COMES BACK. Removing the filter
is only safe while the text stays discarded, so these assertions are written
against what a re-append would actually do: the fact would be stated twice in
the merged evidence, and the legacy list would carry the sentence again.
Verified by mutation, not asserted -- re-adding `evidence.append(_text)` at
the gnomAD call site turns this file red.
"""

import unittest

from pipeline.interpretation import InterpretationEngine

# A real gnomAD miss: the lookup completed and the variant was not there.
# This is the input that used to produce the duplicate.
_GNOMAD_NOT_FOUND = {"skipped": False, "found": False}

_VARIANT_DICT = {
    "chrom": "3",
    "pos": 10146594,
    "ref": "A",
    "alt": "AA",
    "variant_type": "INDEL",
    "id": ".",
    "filter": "PASS",
}


def _pm2_own_evidence(result):
    """PM2's own criterion-level sentences, READ AT RUN TIME from the ACMG
    evaluation this very call produced.

    Not hard-coded: a hard-coded copy would turn this file red for a harmless
    reword of PM2's sentence, which is the trap the gnomAD card just came
    from -- a test that only watches how something is DESCRIBED is defeated
    by renaming it, and fires when nobody meant anything by it.
    """
    acmg = result.get("acmg_evaluation") or {}
    for rule in acmg.get("triggered_criteria") or []:
        if rule.get("code") == "PM2":
            return list(rule.get("supporting_evidence") or [])
    return []


def _legacy_gnomad_sentence(gnomad_result):
    """The sentence `_gnomad_acmg_evidence` builds for this gnomAD dict, READ
    AT RUN TIME.

    This is the exact string whose reappearance is the hazard, and taking it
    from the function rather than copying it here is what keeps a reword from
    deciding the outcome: a hard-coded copy would go GREEN on a reword for
    the wrong reason (it would be looking for a string nothing produces any
    more), which is the failure mode this whole card is about.
    """
    evidence = InterpretationEngine._gnomad_acmg_evidence(gnomad_result)
    assert len(evidence) == 1, evidence
    return evidence[0][0]


class TestTheGnomadMissIsStatedOnceAndByPm2(unittest.TestCase):
    def setUp(self):
        self.result = InterpretationEngine().interpret(
            _VARIANT_DICT, [], {}, {}, {}, {}, gnomad_result=_GNOMAD_NOT_FOUND
        )
        self.merged = (self.result.get("interpretation_result") or {}).get("supporting_evidence") or []

    def test_pm2_states_the_miss_and_states_it_once(self):
        pm2 = _pm2_own_evidence(self.result)
        self.assertTrue(pm2, "PM2 did not trigger for a confirmed gnomAD absence")
        for line in pm2:
            self.assertEqual(
                self.merged.count(line),
                1,
                f"PM2's own evidence line is stated {self.merged.count(line)} times: {line!r}",
            )

    def test_the_superseded_legacy_sentence_reaches_neither_list(self):
        # The duplicate itself. Both surfaces are checked because the
        # deleted `_SUPERSEDED_LEGACY_EVIDENCE` filter only ever touched the
        # merged one -- `report_generator.py::_render_interpretation` renders
        # the LEGACY list directly (:1391), so a re-append would have reached
        # the Markdown report whether or not that filter existed.
        sentence = _legacy_gnomad_sentence(_GNOMAD_NOT_FOUND)
        self.assertNotIn(
            sentence,
            self.merged,
            "the legacy gnomAD sentence is being appended again: it restates PM2's own fact in "
            "different words, so exact-string _dedupe cannot merge them and a reader sees it twice",
        )
        self.assertNotIn(sentence, self.result.get("supporting_evidence") or [])

    def test_unrelated_legacy_evidence_still_reaches_the_merged_result(self):
        # Control: the two assertions above are about the gnomAD sentence
        # specifically, not about the legacy list being dropped wholesale --
        # which is what removing the filter would look like if it had gone
        # too far.
        legacy = self.result.get("supporting_evidence") or []
        self.assertTrue(legacy, "the legacy evidence list should not be empty for this variant")
        for line in legacy:
            self.assertIn(line, self.merged)


if __name__ == "__main__":
    unittest.main()
