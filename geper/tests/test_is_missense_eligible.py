"""
Tests for `pipeline/router.py::SequenceRouter.is_missense_eligible` --
the gate deciding whether AlphaMissense runs for a variant.

This was the last consumer of `pipeline/protein_translator.py`'s
frame-unaware local translation window (flat +/-500bp, no splicing, no
reverse-complement for minus-strand transcripts) after BP7/BP1 were
already moved onto the transcript-CDS-frame `protein_effect_flags`
machinery (see `tests/test_bp1_bp3_bp6_bp7.py`'s module docstring for
that history). `is_missense_eligible` now classifies consequence the
same way, via `transcript_result` instead of ESM-2's translated ref/alt
protein strings.

Reuses the exact same real, live-fetched transcript fixtures and
ClinVar-confirmed variant coordinates `tests/test_bp1_bp3_bp6_bp7.py`
already validates `protein_effect_flags` against, rather than
constructing new synthetic ones -- ground truth, not guesses.
"""

import json
import os
import unittest

from pipeline.router import SequenceRouter
from pipeline.vcf_parser import Variant

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

with open(os.path.join(_FIXTURE_DIR, "ps1_pm5_transcript.json"), "r", encoding="utf-8") as _fh:
    _TP53_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "pvs1_transcripts.json"), "r", encoding="utf-8") as _fh:
    _PVS1_TRANSCRIPTS = json.load(_fh)["transcripts"]


def tp53_transcript_result():
    return {"skipped": False, "found": True, "transcript": _TP53_RECORD}


def gene_transcript_result(gene):
    return {"skipped": False, "found": True, "transcript": _PVS1_TRANSCRIPTS[gene]}


def _variant(chrom, pos, ref, alt, variant_type="SNV", info=None):
    return Variant(
        chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt,
        qual=None, filter_status=".", info=info or {},
    )


# Real: TP53 p.Arg248Trp = c.742C>T, genomic 17:7674221 G>A -- ClinVar
# Pathogenic, expert panel. Confirmed via coding_consequence_detail:
# codon 248, R->W (see test_bp1_bp3_bp6_bp7.py).
_TP53_R248W = _variant("17", 7674221, "G", "A")

# Real: TP53 p.Arg175= (synonymous) = c.525C>T, genomic 17:7675087 G>A.
# Confirmed: codon 175, R->R.
_TP53_SYNONYMOUS = _variant("17", 7675087, "G", "A")

# Real: BRCA1 codon 50, K->* (nonsense), confirmed against the real
# BRCA1 transcript CDS (test_bp1_bp3_bp6_bp7.py::_BRCA1_NONSENSE).
_BRCA1_NONSENSE = _variant("17", 43106520, "T", "A")

# Real: BRCA1 p.Ser1613Gly = c.4837A>G, genomic 17:43071077 T>C (minus
# strand) -- VCV000041827, Benign, expert panel. Confirmed: codon 1613,
# S->G. A second, independent real missense example on a minus-strand
# transcript.
_BRCA1_S1613G = _variant("17", 43071077, "T", "C")


class TestIsMissenseEligibleRealTranscriptClassification(unittest.TestCase):
    def test_real_missense_is_eligible(self):
        router = SequenceRouter()
        self.assertTrue(router.is_missense_eligible(_TP53_R248W, tp53_transcript_result()))

    def test_real_missense_on_minus_strand_transcript_is_eligible(self):
        router = SequenceRouter()
        self.assertTrue(router.is_missense_eligible(_BRCA1_S1613G, gene_transcript_result("BRCA1")))

    def test_real_synonymous_is_not_eligible(self):
        router = SequenceRouter()
        self.assertFalse(router.is_missense_eligible(_TP53_SYNONYMOUS, tp53_transcript_result()))

    def test_real_nonsense_is_not_eligible(self):
        router = SequenceRouter()
        self.assertFalse(router.is_missense_eligible(_BRCA1_NONSENSE, gene_transcript_result("BRCA1")))

    def test_in_frame_indel_is_not_eligible(self):
        # Real: TP53 c.792_794del (p.Leu265del), in-frame deletion --
        # AlphaMissense only scores single-residue substitutions.
        router = SequenceRouter()
        indel = _variant("17", 7673826, "AGTAG", "AG", variant_type="deletion")
        self.assertFalse(router.is_missense_eligible(indel, tp53_transcript_result()))

    def test_frameshift_is_not_eligible(self):
        router = SequenceRouter()
        frameshift = _variant("17", 7673826, "AG", "A", variant_type="deletion")
        self.assertFalse(router.is_missense_eligible(frameshift, tp53_transcript_result()))


class TestIsMissenseEligibleVcfLevelGuards(unittest.TestCase):
    """Guards that must short-circuit before any transcript classification is attempted."""

    def test_non_snv_variant_type_is_not_eligible(self):
        router = SequenceRouter()
        variant = _variant("17", 7674221, "G", "AT", variant_type="insertion")
        self.assertFalse(router.is_missense_eligible(variant, tp53_transcript_result()))

    def test_symbolic_alt_allele_is_not_eligible(self):
        router = SequenceRouter()
        variant = _variant("17", 7674221, "G", "<DEL>")
        self.assertFalse(router.is_missense_eligible(variant, tp53_transcript_result()))

    def test_svtype_flagged_record_is_not_eligible(self):
        router = SequenceRouter()
        variant = _variant("17", 7674221, "G", "A", info={"SVTYPE": "DEL"})
        self.assertFalse(router.is_missense_eligible(variant, tp53_transcript_result()))


class TestIsMissenseEligibleUndeterminedConsequence(unittest.TestCase):
    """
    Undetermined consequence (no usable transcript data) must resolve
    to False -- skip AlphaMissense -- never run it on a guess. See
    `is_missense_eligible`'s docstring for the reasoning.
    """

    def test_no_transcript_result_is_not_eligible(self):
        router = SequenceRouter()
        self.assertFalse(router.is_missense_eligible(_TP53_R248W, None))

    def test_skipped_transcript_lookup_is_not_eligible(self):
        router = SequenceRouter()
        self.assertFalse(router.is_missense_eligible(_TP53_R248W, {"skipped": True}))

    def test_errored_transcript_lookup_is_not_eligible(self):
        router = SequenceRouter()
        result = {"skipped": False, "error": "boom", "found": False}
        self.assertFalse(router.is_missense_eligible(_TP53_R248W, result))

    def test_variant_outside_fetched_transcript_is_not_eligible(self):
        router = SequenceRouter()
        result = dict(tp53_transcript_result(), variant_outside_transcript=True)
        self.assertFalse(router.is_missense_eligible(_TP53_R248W, result))

    def test_intronic_position_is_not_eligible(self):
        # Real: TP53 intronic position 17:7676450 -- falls between two
        # of the fixture's real exons (7676382-7676403 and
        # 7676521-7676622), outside every coding span, so codon_at
        # itself returns None.
        router = SequenceRouter()
        intronic = _variant("17", 7676450, "G", "A")
        self.assertFalse(router.is_missense_eligible(intronic, tp53_transcript_result()))


if __name__ == "__main__":
    unittest.main()
