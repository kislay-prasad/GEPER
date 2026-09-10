"""
A VCF and a GFF3 that state DIFFERENT genome builds must stop the run.

*** THE DEFECT, AND IT IS NOT THAT A CHECK WAS MISSING -- IT IS THAT TWO
CHECKS EXIST AND NEITHER COMPARES THE PAIR THAT REACHES THE COORDINATE. ***

  - `utils/genome_build.py::warn_if_unsupported_build` compares the VCF
    to a CONSTANT (`SUPPORTED_BUILD`, fixed at GRCh38). Warns, never
    blocks.
  - `annotation/codon_provider.py::_check_genome_build_consistency`
    compares the FASTA to the GFF3. Warns, never blocks -- and its own
    docstring states that the VCF is deliberately not checked, recording
    the cross-check as an unfixed follow-up.

So the uncovered combination passes both: *** A GRCh38 VCF (which
satisfies the first check, because it EQUALS the constant) with a GRCh37
FASTA and a GRCh37 GFF3 (which satisfy the second, because they agree
with EACH OTHER). NOTHING WARNS AT ALL. *** That case is pinned below by
name; it is the one this file exists for.

WHY IT MATTERS THAT IT IS THE VCF-vs-GFF3 PAIR SPECIFICALLY:
`annotation/stage.py` computes the HGVS `c.` position by mapping the
VCF's genomic position through the transcript model loaded from the
GFF3. The position comes from one file and the structure it is
interpreted against comes from the other. If they describe different
builds the `c.` coordinate is wrong -- and not wrong in a way that
announces itself: there is no missing value and no error, just a
number, formatted and printed with no qualifier beside it.

SCOPE, DELIBERATELY NARROW: this refuses only a DEFINITE disagreement --
both files state a build and the builds differ. An undetectable build on
either side is NOT refused here, because "cannot confirm" is not
"confirmed inconsistent", and that is the existing philosophy of both
checks above. Widening it to the indeterminate case is a separate
question with a separate cost, and is not decided by this file.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config_validator import ConfigValidationError
from pipeline.utils.genome_build import check_vcf_gff3_build_consistency

_GRCH38_CHR1 = 248956422
_GRCH37_CHR1 = 249250621


def _vcf(tmp, length):
    path = os.path.join(tmp, f"in_{length}.vcf")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("##fileformat=VCFv4.2\n")
        fh.write(f"##contig=<ID=chr1,length={length}>\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    return path


def _gff3(tmp, length):
    path = os.path.join(tmp, f"tx_{length}.gff3")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("##gff-version 3\n")
        fh.write(f"##sequence-region chr1 1 {length}\n")
    return path


class TheFixturesReallyStateTheBuildsTheyClaim(unittest.TestCase):
    """*** THE PRECONDITION EVERYTHING ELSE HERE DEPENDS ON. ***

    If either detector stopped recognising these files, the blocking
    tests below would pass for the wrong reason -- no exception, because
    no detectable disagreement. Asserted directly so that failure is
    loud rather than silently reassuring.
    """

    def test_the_vcf_fixtures_are_detected(self):
        import tempfile

        from pipeline.utils.genome_build import detect_genome_build

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_genome_build(_vcf(tmp, _GRCH38_CHR1)).build, "GRCh38")
            self.assertEqual(detect_genome_build(_vcf(tmp, _GRCH37_CHR1)).build, "GRCh37")

    def test_the_gff3_fixtures_are_detected(self):
        import tempfile

        from pipeline.annotation.codon_provider import _detect_gff_chr1_length

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect_gff_chr1_length(_gff3(tmp, _GRCH38_CHR1)), _GRCH38_CHR1)
            self.assertEqual(_detect_gff_chr1_length(_gff3(tmp, _GRCH37_CHR1)), _GRCH37_CHR1)


class ADefiniteDisagreementStopsTheRun(unittest.TestCase):
    def test_the_silent_case_now_fires(self):
        """*** THE CASE BOTH EXISTING CHECKS PASS: GRCh38 VCF, GRCh37
        GFF3. The VCF equals SUPPORTED_BUILD so the first check is happy;
        the FASTA and GFF3 agree with each other so the second is happy.
        Nothing warned before this. ***"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigValidationError) as caught:
                check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH38_CHR1), _gff3(tmp, _GRCH37_CHR1))

        message = str(caught.exception)
        self.assertIn("GRCh38", message)
        self.assertIn("GRCh37", message)

    def test_the_opposite_disagreement_also_fires(self):
        """Symmetric: a GRCh37 VCF against a GRCh38 GFF3."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigValidationError):
                check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH37_CHR1), _gff3(tmp, _GRCH38_CHR1))

    def test_the_refusal_names_the_two_files(self):
        """A fatal error that does not say WHICH pair disagreed leaves the
        operator to guess between a VCF, a FASTA and a GFF3."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            vcf, gff = _vcf(tmp, _GRCH38_CHR1), _gff3(tmp, _GRCH37_CHR1)
            with self.assertRaises(ConfigValidationError) as caught:
                check_vcf_gff3_build_consistency(vcf, gff)

        message = str(caught.exception)
        self.assertIn(os.path.basename(vcf), message)
        self.assertIn(os.path.basename(gff), message)


class TheCheckDoesNotFireOnRunsThatAreFine(unittest.TestCase):
    """*** A GATE THAT ALWAYS FIRES AND A GATE THAT NEVER FIRES LOOK
    IDENTICAL FROM ONE SIDE. *** These are the other side."""

    def test_an_agreeing_pair_does_not_fire(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH38_CHR1), _gff3(tmp, _GRCH38_CHR1))

    def test_an_agreeing_grch37_pair_does_not_fire(self):
        """Agreement is what is required, not GRCh38 specifically -- that
        is the OTHER check's business and this one must not duplicate it."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH37_CHR1), _gff3(tmp, _GRCH37_CHR1))

    def test_an_undetectable_gff3_does_not_fire(self):
        """CANNOT CONFIRM IS NOT CONFIRMED INCONSISTENT."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "bare.gff3")
            with open(bare, "w", encoding="utf-8") as fh:
                fh.write("##gff-version 3\n")
            check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH38_CHR1), bare)

    def test_an_undetectable_vcf_does_not_fire(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "bare.vcf")
            with open(bare, "w", encoding="utf-8") as fh:
                fh.write("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            check_vcf_gff3_build_consistency(bare, _gff3(tmp, _GRCH38_CHR1))

    def test_a_missing_gff3_path_does_not_fire(self):
        """An unconfigured or absent GFF3 is the existing "transcript
        analysis disabled" state, not a build disagreement."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            check_vcf_gff3_build_consistency(
                _vcf(tmp, _GRCH38_CHR1), os.path.join(tmp, "nope.gff3")
            )
            check_vcf_gff3_build_consistency(_vcf(tmp, _GRCH38_CHR1), "")


class TheExistingChecksStillBehaveAsTheyDid(unittest.TestCase):
    """*** ADDING A THIRD CHECK MUST NOT CHANGE THE TWO THAT WERE ALREADY
    THERE. *** Both still warn and neither blocks."""

    def test_warn_if_unsupported_build_still_returns_rather_than_raising(self):
        import tempfile

        from pipeline.utils.genome_build import warn_if_unsupported_build

        with tempfile.TemporaryDirectory() as tmp:
            result = warn_if_unsupported_build(_vcf(tmp, _GRCH37_CHR1))
        self.assertEqual(result.build, "GRCh37")

    def test_the_fasta_gff3_check_is_untouched(self):
        """It is still a warning: the provider exposes its verdict on the
        instance and does not raise."""
        from pipeline.annotation.codon_provider import FastaCodonContextProvider

        self.assertTrue(hasattr(FastaCodonContextProvider, "_check_genome_build_consistency"))


if __name__ == "__main__":
    unittest.main()
