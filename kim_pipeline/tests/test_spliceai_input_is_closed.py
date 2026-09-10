"""
SpliceAI-derived scores must not reach an ACMG classification.

*** THE CLAIM UNDER TEST IS NOT "THE FIELDS ARE PARSED". IT IS "A
SPLICEAI-DERIVED SCORE CHANGES A CLASSIFICATION". *** A test that only
shows `DS_AG` being read would go green the moment someone renamed a
variable, while the vote it feeds carried on moving. So every assertion
below is made on a CLASSIFICATION OUTCOME, and the two arms of each
comparison differ by NOTHING except the presence of the SpliceAI delta
fields in the input VCF's INFO column.

WHY THE FIELDS SURVIVED THE FIRST REMOVAL, WHICH IS THE REASON THIS FILE
TARGETS THE INPUT AND NOT A ROUTE: the 2026-08-22 licence remedy removed
SpliceAI from VEP's plugin request, which makes `_extract_csq_scores`'s
third tuple slot ALWAYS `None` (stage.py:450-455, verified: `best_spliceai`
is never assigned). But both call sites read that `None` and then fall
back to the raw INFO column:

    var.spliceai_score = csq_spliceai        # always None since 2026-08-22
    if var.spliceai_score is None:           # ... so this ALWAYS runs
        <parse SpliceAI= / DS_AG / DS_AL / DS_DG / DS_DL, take the max>

*** THE REMEDY IS WHAT GUARANTEES THE FALLBACK EXECUTES. Removing the
VEP route did not narrow the input; it handed the input to the fallback.
That is why closing another route would not close this, and why the
assertions here are written against the INPUT. ***

HOW A SPLICEAI SCORE MOVES A VOTE -- TWO MECHANISMS, NOT ONE, AND THE
SECOND IS THE ONE THAT GETS MISSED. `_pp3`/`_bp4` (classifier.py:809-832,
1166-1176) count votes and require a majority:

    met = n_dam >= max(1, len(votes) // 2 + 1)

So a SpliceAI score changes the outcome BOTH by voting AND by changing
the DENOMINATOR -- it moves the bar from 2-of-3 to 3-of-4. *** A SPLICEAI
SCORE THAT VOTES *BENIGN* CAN THEREFORE FLIP PP3 OFF WITHOUT EVER
AGREEING WITH THE PATHOGENIC SIDE. *** Both mechanisms are pinned below,
because closing only the first would leave the evidence bar still varying
per input.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
from pipeline.annotation.stage import _parse_vcf

_VCF_HEADER = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"

#: Fixed non-SpliceAI context, IDENTICAL in both arms of every comparison
#: below. CADD 25.0 votes damaging (>= 20.0); REVEL 0.30 votes benign
#: (< 0.50). Without any SpliceAI score that is 1 damaging of 2 votes,
#: which fails the majority rule -- so PP3 is off, and any change to PP3
#: between the two arms is attributable to the SpliceAI input alone.
_INFO_BASE = "CADD_PHRED=25.0;REVEL=0.30"


def _parse_info(info: str):
    """Parse a one-record VCF carrying `info` and return the variant."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "variant.vcf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_VCF_HEADER)
            handle.write(f"17\t43057051\t.\tA\tT\t100\tPASS\t{info}\n")
        variants = _parse_vcf(path)
    assert len(variants) == 1, f"fixture parsed to {len(variants)} variants, expected 1"
    return variants[0]


def _classify_from_info(info: str):
    """VCF INFO -> parsed variant -> ACMG result, through the real parser.

    Only the in-silico scores are taken from the parse; the remaining
    evidence is fixed and identical across arms, so the comparison
    isolates the SpliceAI input.
    """
    var = _parse_info(info)
    evidence = VariantEvidence(
        chrom="17",
        pos=43057051,
        ref="A",
        alt="T",
        gene="BRCA1",
        is_missense=True,
        cadd_phred=var.cadd_phred,
        revel_score=var.revel_score,
        spliceai_score=var.spliceai_score,
        alphamissense_score=var.alphamissense_score,
    )
    return AcmgClassifier(cfg={}).classify(evidence)


class SpliceAiScoresDoNotReachAClassification(unittest.TestCase):
    """*** THE VOTE MUST NOT MOVE. *** Asserted on outcomes, not on parsing."""

    def test_delta_fields_do_not_change_the_pp3_call(self):
        """DS_AG at 0.95 must not turn PP3 on.

        Damaging votes go 1-of-2 (fails majority) to 2-of-3 (passes) purely
        because a SpliceAI score joined the ballot. This is the licensed
        score changing a reported ACMG criterion.
        """
        without = _classify_from_info(_INFO_BASE)
        with_ds = _classify_from_info(f"{_INFO_BASE};DS_AG=0.95")

        self.assertEqual(
            "PP3" in with_ds.criteria_met,
            "PP3" in without.criteria_met,
            "a SpliceAI DS_AG score changed whether PP3 is met -- the licensed "
            "score is still reaching the ACMG vote",
        )

    def test_a_benign_spliceai_score_does_not_change_the_pp3_call_either(self):
        """*** THE DENOMINATOR MECHANISM, WHICH A VOTE-ONLY FIX WOULD MISS. ***

        DS_AG=0.01 votes BENIGN, so it never agrees with the pathogenic
        side -- yet it still enlarges the ballot and moves the majority
        bar. Pinning only the damaging direction would leave the evidence
        bar varying per input, which is the defect Andy measured (3-of-4
        when the VCF carries DS_*, 2-of-3 when it does not).

        MEASURED, NOT ASSUMED, AND THE FIRST FIXTURE I WROTE HERE PROVED
        NOTHING: the bar is `len(votes) // 2 + 1`, which is unchanged from
        2 votes to 3 -- so a CADD+REVEL baseline passes in BOTH arms and
        the test could not fail. It has to cross a bar to demonstrate
        anything, and 1 vote -> 2 does (bar 1 -> 2). With CADD alone,
        adding a benign SpliceAI score turns PP3 from MET to NOT MET.
        """
        without = _classify_from_info("CADD_PHRED=25.0")
        with_ds = _classify_from_info("CADD_PHRED=25.0;DS_AG=0.01")

        self.assertEqual(
            "PP3" in with_ds.criteria_met,
            "PP3" in without.criteria_met,
            "a BENIGN SpliceAI score changed whether PP3 is met, by changing the "
            "number of votes rather than by agreeing with either side",
        )

    def test_every_delta_field_is_closed_not_just_the_first(self):
        """All four delta fields, each alone.

        The parser takes the MAX of whichever are present, so closing only
        `DS_AG` would leave three live inputs. Named individually so a
        partial fix cannot pass.
        """
        baseline = _classify_from_info(_INFO_BASE)
        for field in ("DS_AG", "DS_AL", "DS_DG", "DS_DL"):
            with self.subTest(field=field):
                result = _classify_from_info(f"{_INFO_BASE};{field}=0.95")
                self.assertEqual(
                    "PP3" in result.criteria_met,
                    "PP3" in baseline.criteria_met,
                    f"{field} alone still reaches the ACMG vote",
                )

    def test_the_spliceai_info_key_is_closed_too(self):
        """`SpliceAI=` is checked BEFORE the delta fields and is the same input."""
        baseline = _classify_from_info(_INFO_BASE)
        result = _classify_from_info(f"{_INFO_BASE};SpliceAI=0.95")
        self.assertEqual(
            "PP3" in result.criteria_met,
            "PP3" in baseline.criteria_met,
            "the SpliceAI= INFO key still reaches the ACMG vote",
        )

    def test_the_score_is_absent_from_the_parsed_variant(self):
        """The input itself, asserted last rather than first.

        This is the mechanism, not the claim -- it is here so a failure
        localises to the parser instead of to the classifier, and it is
        deliberately NOT the only assertion in this file.
        """
        self.assertIsNone(_parse_info(f"{_INFO_BASE};DS_AG=0.95").spliceai_score)


class SpliceAiIsNotConsultedEvenWhenAScoreIsForced(unittest.TestCase):
    """
    *** THE ONE CASE THE INPUT-LEVEL TESTS ABOVE CANNOT REACH: what
    happens if a `spliceai_score` arrives on the evidence object anyway. ***
    Everything above drives the classifier through `_parse_vcf`, so it can
    only ever assert on inputs the pipeline actually produces. This class
    sets the field DIRECTLY, which is the one way the old vote branches
    could still have fired.

    HISTORY, BECAUSE THIS TEST USED TO ASSERT THE OPPOSITE. It lived in
    `test_acmg_audit_issue4.py` as
    `test_spliceai_still_counted_regardless_of_missense_status` and
    asserted that a forced score of 0.9 DID reach PP3. It passed -- and it
    could only ever pass, because it supplied an input production cannot
    produce and then checked that the unreachable branch fired.
    *** A TEST WHOSE SUBJECT CANNOT OCCUR IN PRODUCTION IS NOT TESTING THE
    PRODUCT, IT IS PINNING AN IMPLEMENTATION. *** It was the only thing
    standing between the dead PP3/BP4 SpliceAI votes and their removal.

    Converted rather than deleted: flipped, it asserts the property the
    removal creates, and it CAN FAIL -- if anyone re-adds a SpliceAI vote,
    or re-opens the input in a way the parse-level tests miss, this goes
    red. Its predecessor could not go red for any reason a clinician would
    care about.
    """

    def _classify_forced(self, **overrides):
        ev = VariantEvidence(chrom="17", pos=1, ref="A", alt="T", gene="TESTGENE", **overrides)
        return AcmgClassifier(cfg={}).classify(ev)

    def test_spliceai_is_absent_from_pp3_regardless_of_missense_status(self):
        """A forced damaging SpliceAI score must not appear in PP3.

        Non-missense on purpose: PP3's SpliceAI vote was deliberately NOT
        gated on `is_missense` (unlike REVEL and AlphaMissense), so this is
        the arm where the old branch was reachable.
        """
        result = self._classify_forced(is_missense=False, spliceai_score=0.9)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        self.assertNotIn(
            "SpliceAI",
            pp3.reason,
            "SpliceAI reached PP3 from a forced score -- a SpliceAI vote has been "
            "re-added to the classifier, or the input is open again",
        )

    def test_spliceai_is_absent_from_bp4_regardless_of_missense_status(self):
        """The benign arm, which the vote-count mechanism makes separate:
        a SpliceAI score also changes the DENOMINATOR of the majority, so a
        benign-voting score could flip a call without ever agreeing with the
        pathogenic side (see this module's docstring)."""
        result = self._classify_forced(is_missense=False, spliceai_score=0.01)
        bp4 = next(c for c in result.all_criteria if c.code == "BP4")
        self.assertNotIn(
            "SpliceAI",
            bp4.reason,
            "SpliceAI reached BP4 from a forced score -- a SpliceAI vote has been "
            "re-added to the classifier, or the input is open again",
        )

    def test_a_forced_score_does_not_change_the_pp3_outcome_either(self):
        """Not just the reason string: the OUTCOME must be identical with
        and without the forced score. Asserting only on `reason` would go
        green if the vote were re-added under a different label."""
        without = self._classify_forced(is_missense=False, cadd_phred=25.0)
        with_forced = self._classify_forced(is_missense=False, cadd_phred=25.0, spliceai_score=0.9)
        pp3_a = next(c for c in without.all_criteria if c.code == "PP3")
        pp3_b = next(c for c in with_forced.all_criteria if c.code == "PP3")
        self.assertEqual(
            (pp3_a.status, pp3_a.reason),
            (pp3_b.status, pp3_b.reason),
            "a forced SpliceAI score changed the PP3 call -- it is being consulted",
        )


class LegitimateNonSpliceAiEvidenceStillContributes(unittest.TestCase):
    """
    *** THE CONTROL, AND IT IS THE HALF THAT A GREEN RUN ALONE CANNOT
    DISTINGUISH: REMOVING THE SPLICEAI CONTRIBUTION AND BREAKING PP3/BP4
    OUTRIGHT LOOK IDENTICAL IF YOU ONLY ASSERT THAT SPLICEAI NO LONGER
    MOVES THE VOTE. *** These pin that the OTHER predictors still decide
    exactly as they did.
    """

    def test_pp3_still_fires_on_non_spliceai_predictors_alone(self):
        """CADD + REVEL both damaging -> PP3 met, with no SpliceAI anywhere."""
        result = _classify_from_info("CADD_PHRED=25.0;REVEL=0.90")
        self.assertIn(
            "PP3",
            result.criteria_met,
            "PP3 no longer fires on CADD+REVEL alone -- the deletion damaged the "
            "surviving predictors rather than only removing SpliceAI",
        )

    def test_bp4_still_fires_on_non_spliceai_predictors_alone(self):
        """CADD + REVEL both benign -> BP4 met, with no SpliceAI anywhere."""
        result = _classify_from_info("CADD_PHRED=5.0;REVEL=0.05")
        self.assertIn(
            "BP4",
            result.criteria_met,
            "BP4 no longer fires on CADD+REVEL alone -- the deletion damaged the "
            "surviving predictors rather than only removing SpliceAI",
        )

    def test_the_other_info_scores_are_still_parsed(self):
        """CADD / REVEL / AlphaMissense must survive untouched.

        Guards the blast radius of an edit inside the same INFO-parsing
        block: it would be easy to remove a neighbouring line.
        """
        var = _parse_info("CADD_PHRED=25.0;REVEL=0.30;AM_PATHOGENICITY=0.80")
        self.assertEqual(var.cadd_phred, 25.0)
        self.assertEqual(var.revel_score, 0.30)
        self.assertEqual(var.alphamissense_score, 0.80)

    def test_pp3_reports_not_evaluated_when_no_predictors_are_present(self):
        """The fail-safe direction is preserved: absence is 'not evaluated',
        never a confirmed benign result."""
        result = _classify_from_info("DP=30")
        self.assertNotIn("PP3", result.criteria_met)
        self.assertNotIn("BP4", result.criteria_met)


if __name__ == "__main__":
    unittest.main()
