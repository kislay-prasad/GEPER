"""GFF3 phase != 0 flipped a clinical classification TIER, not just a codon.

THIS TEST PINS THE TIER, DELIBERATELY, AND A CODON-LEVEL TEST WOULD NOT DO.
`codon_provider.py` read every codon of every transcript whose first CDS exon
has phase != 0 from `phase` bases upstream of the true codon. The reason that
mattered is not that a string was wrong -- it is the chain it feeds:

    codon_provider  a true start_lost is reported as a missense
      -> shared.py:315-323   is_lof True->False, is_missense False->True
      -> classifier.py::_pvs1  the highest-weighted criterion (8.0) MET -> NOT MET
      -> with PM2 also present, the LABEL flips
             Likely_Pathogenic  ->  Uncertain_Significance
      -> both renderers read .get("classification") off that dict unconditionally

Nothing downstream re-derives the consequence, so nothing downstream can notice.
A unit test on `codon_index` would have gone green while a real report carried
the wrong tier, which is why this drives THE WHOLE PATH: the real
`FastaCodonContextProvider` against a real (temp-file) GFF3 + FASTA, the
is_lof/is_missense derivation copied from the real `shared.py` sets, the real
`AcmgClassifier`, and an assertion on `AcmgResult.classification`.

THE FIXTURE IS A START CODON ON PURPOSE. With phase=1 on a CDS exon starting at
101, the first complete codon is 102-104 = ATG. A>T at 102 destroys the
initiator. Read one base upstream -- 101-103 = GAT -- it looks like an ordinary
D>Y missense. The two readings are both syntactically valid codons, which is
exactly why the defect produced plausible output rather than an error.
"""

import os
import tempfile
import unittest
from unittest import mock

import pipeline.annotation.codon_provider as codon_provider_module
from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
from pipeline.annotation.codon_provider import CdsRecord, FastaCodonContextProvider


# chr1 CDS exon 101..130, + strand, GFF3 phase=1.
#   1-based: 101 102 103 104 105 ...
#            G   A   T   G   C
# phase=1 -> first complete codon starts at 102 -> ATG (an initiator).
_SEQUENCE = ("N" * 100) + "GATGCATGGATCCATGAATTCCGGATCCTAGGAA" + ("N" * 20)
_TRANSCRIPT = "NM_PHASE_START.1"

# Copied verbatim from pipeline/orchestration/shared.py:315-323. Duplicated
# rather than imported because the point is to reproduce what the orchestrator
# DOES with a consequence string; if shared.py's set ever changes, this test
# should be reviewed rather than silently following it.
_LOF_CONSEQUENCES = {
    "stop_gained",
    "stop_lost",
    "start_lost",
    "frameshift_variant",
    "splice_donor_variant",
    "splice_acceptor_variant",
}

# codon_provider returns short names; annotation/stage.py maps them onto the
# vocabulary shared.py keys on (stage.py:146, :153-154).
_CONSEQUENCE_ALIASES = {"missense": "missense_variant"}


class TestPhaseShiftFlipsTheClassificationTier(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.gff_path = os.path.join(tmp, "phase_start.gff3")
        with open(self.gff_path, "w", encoding="utf-8") as fh:
            fh.write("chr1\tsrc\tCDS\t101\t130\t.\t+\t1\tParent=%s\n" % _TRANSCRIPT)
        self.fasta_path = os.path.join(tmp, "phase_start.fasta")
        with open(self.fasta_path, "w", encoding="utf-8") as fh:
            fh.write(">chr1\n%s\n" % _SEQUENCE)

    def tearDown(self):
        self._tmp.cleanup()

    # -- the path itself ----------------------------------------------------

    def _consequence_from_the_real_provider(self):
        """One real run of the real provider. Returns the orchestrator's
        vocabulary, not the provider's."""
        with mock.patch.object(
            codon_provider_module._FastaReader, "_check_samtools", staticmethod(lambda: False)
        ):
            provider = FastaCodonContextProvider(gff_path=self.gff_path, fasta_path=self.fasta_path)
            self.assertTrue(
                provider._available,
                "fixture GFF3/FASTA must load, or this test proves nothing",
            )
            provider._cds_map = {_TRANSCRIPT: [CdsRecord("chr1", 101, 130, "+", 1, _TRANSCRIPT)]}
            result = provider.get_codon_and_aa("chr1", 102, "A", "T", _TRANSCRIPT)

        self.assertIsNotNone(result, "provider returned nothing for an in-CDS SNV")
        consequence = result[0]
        return _CONSEQUENCE_ALIASES.get(consequence, consequence), result

    @staticmethod
    def _classify(consequence):
        """shared.py's derivation, then the real classifier. PM2 present via
        gnomad_af_absent, exactly as the reported case had it."""
        evidence = VariantEvidence(
            chrom="chr1",
            pos=102,
            ref="A",
            alt="T",
            gene="TESTGENE",
            transcript_id=_TRANSCRIPT,
            is_lof=consequence in _LOF_CONSEQUENCES,
            is_missense=consequence == "missense_variant",
            lof_gene_intolerant=True,
            gnomad_af_absent=True,
        )
        return AcmgClassifier().classify(evidence), evidence

    # -- the assertion that matters -----------------------------------------

    def test_the_start_codon_variant_classifies_as_likely_pathogenic(self):
        """THE TIER. Every step is the real one; nothing is hand-fed."""
        consequence, raw = self._consequence_from_the_real_provider()
        self.assertEqual(
            consequence,
            "start_lost",
            "the real provider did not see the initiator codon being destroyed; "
            "it returned %r (codon %r->%r). Read one base upstream this looks "
            "like an ordinary missense." % (raw[0], raw[1], raw[2]),
        )

        result, evidence = self._classify(consequence)
        self.assertTrue(evidence.is_lof, "start_lost must derive is_lof")
        self.assertFalse(evidence.is_missense)
        self.assertIn(
            "PVS1", result.criteria_met, "PVS1 is the 8.0-weighted criterion this turns on"
        )
        self.assertIn("PM2", result.criteria_met, "PM2 must be present or the tier proves nothing")
        self.assertEqual(
            result.classification,
            "Likely_Pathogenic",
            "the clinical tier a report would carry for this variant; got %r with criteria %r"
            % (result.classification, result.criteria_met),
        )

    def test_the_misread_codon_would_have_produced_a_lower_tier(self):
        """THE OTHER HALF, PINNED SO THE TIER FLIP IS VISIBLE IN ONE FILE.

        This does not re-run the defect -- it asserts what the classifier does
        with the consequence the defect PRODUCED ('missense_variant'), so the
        two rows sit side by side. If someone reintroduces the phase bug, the
        test above fails; this one documents exactly what it would fail INTO.
        """
        result, evidence = self._classify("missense_variant")
        self.assertFalse(evidence.is_lof)
        self.assertTrue(evidence.is_missense)
        self.assertNotIn("PVS1", result.criteria_met)
        self.assertEqual(
            result.classification,
            "Uncertain_Significance",
            "the tier the misread codon yielded; got %r" % result.classification,
        )

    def test_the_two_tiers_actually_differ(self):
        """Guards against both branches quietly converging on one label -- at
        which point both assertions above would still pass and the test would
        be pinning nothing."""
        lof_result, _ = self._classify("start_lost")
        missense_result, _ = self._classify("missense_variant")
        self.assertNotEqual(
            lof_result.classification,
            missense_result.classification,
            "the whole finding is that these two differ; if they stop differing "
            "this test no longer detects the defect it was written for",
        )


if __name__ == "__main__":
    unittest.main()
