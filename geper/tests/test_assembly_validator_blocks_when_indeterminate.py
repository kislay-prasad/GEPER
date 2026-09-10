"""
An assembly that cannot be established must STOP the run, not fall
through to whatever Ensembl defaults to.

*** THE DEFECT, IN THE HUMAN'S OWN WORDS (2026-09-10): "A GRCh37 VCF
SILENTLY GETTING GRCh38 TRANSCRIPT STRUCTURE, WARNING BUT NEVER
BLOCKING, IS THE EXACT FAILURE CLASS THE BUILD CHECK EXISTS TO PREVENT,
MOVED ONE LAYER DOWN." *** The check was never missing. It was
relocated below itself: `validate_assembly` blocks hard when the VCF
header and `--assembly` disagree, and then returns `None` -- silently --
when neither can be established at all. The pair that is checked gets a
fatal error; the pair that reaches the coordinates gets a log line.

WHERE THE `None` GOES, which is why a warning is not enough:
`sequence_context.SequenceContextGenerator.__init__` documents its own
`assembly` parameter as "None lets Ensembl use its default", and the
orchestrator hands that value to the transcript lookup that builds
HGVS c. coordinates. So an undetermined build does not produce a
missing value or an error downstream -- it produces a confident wrong
one, in a field a clinician quotes.

THIS OVERTURNS A DELIBERATE, DOCUMENTED DECISION, AND THAT IS THE POINT
OF SAYING SO HERE. This module's own docstring listed, as item 4:
"If detection is inconclusive, warn and proceed -- we should never
block a run just because header metadata was informal or missing;
sequence_context.py's per-variant REF-mismatch warning remains as a
second line of defense." That second line of defense is real
(`sequence_context.py:97` logs "Reference mismatch for ..." per
variant) -- *** BUT IT IS A WARNING, PER VARIANT, ON A RUN THAT HAS
ALREADY FETCHED THE WRONG BUILD. It reports the symptom after the
decision that caused it. *** The human ruled that trade the other way.

THE ESCAPE HATCH IS DELIBERATE AND IS PINNED BELOW: an informal header
is not itself fatal. Passing `--assembly` explicitly still proceeds,
because then the build HAS been established -- by the caller, on
purpose. What is refused is proceeding with the build established by
NOBODY.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.assembly_validator import validate_assembly
from utils.exceptions import AssemblyMismatchError

#: A header with no build tag and no chr1 contig length -- the detector
#: returns None for it. Verified in the first test below rather than
#: assumed, so a change to the detector cannot quietly turn every other
#: test in this file into a no-op.
_INDETERMINATE_HEADER = [
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr7>",
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
]

_GRCH38_HEADER = [
    "##fileformat=VCFv4.2",
    "##reference=GRCh38",
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
]


class TheFixtureIsActuallyIndeterminate(unittest.TestCase):
    """*** THE PRECONDITION EVERY OTHER TEST HERE DEPENDS ON. ***

    If `_INDETERMINATE_HEADER` ever became detectable, the blocking
    tests below would pass for the wrong reason -- no exception, because
    no indeterminate case. Asserted directly so that failure is loud.
    """

    def test_the_indeterminate_fixture_really_is_undetectable(self):
        from pipeline.assembly_validator import detect_vcf_assembly

        self.assertIsNone(detect_vcf_assembly(_INDETERMINATE_HEADER))

    def test_the_detectable_fixture_really_is_detectable(self):
        from pipeline.assembly_validator import detect_vcf_assembly

        self.assertEqual(detect_vcf_assembly(_GRCH38_HEADER), "GRCh38")


class AnUnestablishableAssemblyStopsTheRun(unittest.TestCase):
    def test_indeterminate_header_and_no_assembly_flag_raises(self):
        """The ruling, asserted directly.

        Before this change `validate_assembly` returned `None` here and
        the run continued on Ensembl's default build.
        """
        with self.assertRaises(AssemblyMismatchError) as caught:
            validate_assembly(_INDETERMINATE_HEADER, None)

        message = str(caught.exception)
        self.assertIn("--assembly", message, "the refusal must name the flag that resolves it")

    def test_the_refusal_says_what_to_do_about_it(self):
        """A fatal error that does not name its own remedy just moves the
        problem to whoever reads the traceback. The sibling mismatch
        error names three remedies; this one must name its own."""
        with self.assertRaises(AssemblyMismatchError) as caught:
            validate_assembly(_INDETERMINATE_HEADER, None)

        message = str(caught.exception).lower()
        self.assertTrue(
            "grch38" in message and "grch37" in message,
            "the refusal should name the builds the caller can choose between",
        )


class TheGateDoesNotFireOnRunsThatAreFine(unittest.TestCase):
    """
    *** A GATE THAT ALWAYS FIRES AND A GATE THAT NEVER FIRES LOOK
    IDENTICAL FROM ONE SIDE. *** These are the other side.
    """

    def test_indeterminate_header_with_explicit_assembly_proceeds(self):
        """THE ESCAPE HATCH. An informal header is not fatal by itself --
        the caller has established the build, which is the thing that
        was missing."""
        self.assertEqual(validate_assembly(_INDETERMINATE_HEADER, "GRCh38"), "GRCh38")

    def test_detected_header_and_no_flag_proceeds_with_the_detected_build(self):
        """The ordinary case: nothing was ambiguous, nothing is blocked."""
        self.assertEqual(validate_assembly(_GRCH38_HEADER, None), "GRCh38")

    def test_detected_header_agreeing_with_the_flag_proceeds(self):
        self.assertEqual(validate_assembly(_GRCH38_HEADER, "GRCh38"), "GRCh38")


class TheExistingMismatchBlockIsUnchanged(unittest.TestCase):
    """
    *** THIS MODULE HAD NO TESTS AT ALL BEFORE THIS FILE -- `grep -rln
    "validate_assembly\\|AssemblyMismatchError" tests/` RETURNED ZERO.
    THE HARD BLOCK THAT ALREADY EXISTED WAS UNTESTED. *** Pinned here so
    that adding a second refusal cannot quietly damage the first.
    """

    def test_header_and_flag_disagreeing_still_raises(self):
        with self.assertRaises(AssemblyMismatchError):
            validate_assembly(_GRCH38_HEADER, "GRCh37")

    def test_equivalent_spellings_do_not_count_as_a_disagreement(self):
        """`hg38` and `GRCh38` are the same build spelled differently --
        blocking on that would be a false refusal."""
        self.assertIsNotNone(validate_assembly(_GRCH38_HEADER, "hg38"))


if __name__ == "__main__":
    unittest.main()
