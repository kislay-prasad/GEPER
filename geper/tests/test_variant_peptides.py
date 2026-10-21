"""
Variant-aware ESM-2 peptides (`pipeline/variant_peptides.py`) and the protein stage that uses them.

Background (Run 5, BRCA1 17:43093449 G>T, p.Ser694Arg on ENST00000357654): the old protein stage
translated a flat +/-500 bp genomic window from its first AUG. That open reading frame ended ~430
bases before the variant, so the "reference" and "alternate" peptides were identical and ESM-2 was
handed a peptide unrelated to the variant, yet ESM-2 was still counted as having run.

Expected values here are derived independently of the module under test: hand-built micro-transcripts
whose peptides are written out by hand, and the real BRCA1 transcript record checked against its own
translation with a separately written codon table. The BRCA1 fixture
(`fixtures/transcripts/brca1_enst00000357654_cache_record.json`) was extracted read-only from the local
Ensembl release-116 GTF/CDS cache; its translation was also checked by eye against the published
BRCA1 N-terminus, and the S694R call agrees with the independent AlphaMissense catalogue row
(chr17:43093449 G>T, ENST00000357654.8, P38398, S694R).
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline import variant_peptides as vp
from pipeline.pvs1.models import ExonSpan, TranscriptContext
from pipeline.pvs1.utils import _protein_null_flags, transcript_context_from_dict
from pipeline.variant_peptides import PeptideSkip, VariantPeptides, build_variant_peptides

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts", "brca1_enst00000357654_cache_record.json")

# A separately written standard table (DNA-keyed) -- deliberately not CONFIG.CODON_TABLE.
_B = "TCAG"
_AA = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
_TABLE = {
    a + b + c: _AA[i * 16 + j * 4 + k] for i, a in enumerate(_B) for j, b in enumerate(_B) for k, c in enumerate(_B)
}
_COMP = {"A": "T", "C": "G", "G": "C", "T": "A"}


def _translate(cds: str) -> str:
    return "".join(_TABLE[cds[i : i + 3]] for i in range(0, len(cds), 3))


def _brca1():
    with open(_FIXTURE, encoding="utf-8") as fh:
        record = json.load(fh)
    return record["transcript"], transcript_context_from_dict(record["transcript"])


def _single_exon(cds: str, strand: int, start: int = 1000, protein_length="auto"):
    """One-exon transcript whose CDS is `cds` (transcript orientation) laid out on `strand`."""
    end = start + len(cds) - 1
    return TranscriptContext(
        transcript_id="ENST_TEST",
        gene_symbol="TESTG",
        chrom="1",
        strand=strand,
        exons=[ExonSpan(start=start, end=end, rank=0)],
        cds_genomic_start=start,
        cds_genomic_end=end,
        protein_length=(len(cds) // 3 - 1) if protein_length == "auto" else protein_length,
        cds_sequence=cds,
    )


def _variant_for_cds_index(tx: TranscriptContext, cds_index: int, new_base: str):
    """Genomic (pos, ref, alt) for changing the 0-based CDS base `cds_index` to `new_base` (transcript orientation)."""
    ref_t = tx.cds_sequence[cds_index]
    if tx.strand > 0:
        return tx.cds_genomic_start + cds_index, ref_t, new_base
    return tx.cds_genomic_end - cds_index, _COMP[ref_t], _COMP[new_base]


# ---------------------------------------------------------------------------
# Micro-transcripts with an exon boundary inside a codon
#   CDS = ATG GCT AAA TGG TAA  ->  M A K W *     (codon 3 = AAA is c.7 c.8 c.9)
#   exon 1 carries c.1-7, exon 2 carries c.8-15, so codon 3 straddles the splice junction.
# ---------------------------------------------------------------------------
_CDS_SPLIT = "ATGGCTAAATGGTAA"


def _split_plus():
    # exon1 genomic 100-106 = c.1-7 ; exon2 genomic 200-207 = c.8-15
    return TranscriptContext(
        transcript_id="ENST_PLUS",
        gene_symbol="PLUSG",
        chrom="1",
        strand=1,
        exons=[ExonSpan(100, 106, 0), ExonSpan(200, 207, 0)],
        cds_genomic_start=100,
        cds_genomic_end=207,
        protein_length=4,
        cds_sequence=_CDS_SPLIT,
    )


def _split_minus():
    # transcript 5' exon is the higher one: exon1 genomic 206..200 = c.1-7 ; exon2 genomic 107..100 = c.8-15
    return TranscriptContext(
        transcript_id="ENST_MINUS",
        gene_symbol="MINUSG",
        chrom="1",
        strand=-1,
        exons=[ExonSpan(100, 107, 0), ExonSpan(200, 206, 0)],
        cds_genomic_start=100,
        cds_genomic_end=206,
        protein_length=4,
        cds_sequence=_CDS_SPLIT,
    )


class TestSplicedCodonsOnBothStrands(unittest.TestCase):
    """Codon 3 (AAA, Lys) is split across an exon junction; hand-derived expectations."""

    def _run(self, tx, pos, ref, alt):
        return build_variant_peptides(tx, pos, ref, alt, max_residues=1022)

    def test_plus_strand_boundary_codon_missense_first_base(self):
        # genomic 106 (c.7, last base of exon 1) A>G : AAA -> GAA = Lys->Glu
        out = self._run(_split_plus(), 106, "A", "G")
        self.assertIsInstance(out, VariantPeptides)
        self.assertEqual((out.codon_number, out.ref_aa, out.alt_aa, out.category), (3, "K", "E", "missense"))
        self.assertEqual(out.ref_peptide, "MAKW")
        self.assertEqual(out.alt_peptide, "MAEW")
        self.assertEqual(out.variant_index, 2)

    def test_plus_strand_boundary_codon_missense_second_base(self):
        # genomic 200 (c.8, first base of exon 2) A>G : AAA -> AGA = Lys->Arg
        out = self._run(_split_plus(), 200, "A", "G")
        self.assertEqual((out.ref_aa, out.alt_aa), ("K", "R"))
        self.assertEqual(out.alt_peptide, "MARW")

    def test_plus_strand_boundary_codon_synonymous_third_base(self):
        # genomic 201 (c.9) A>G : AAA -> AAG = Lys->Lys
        out = self._run(_split_plus(), 201, "A", "G")
        self.assertEqual(out.category, "synonymous")
        self.assertFalse(out.protein_changed)
        self.assertEqual(out.ref_peptide, out.alt_peptide)
        self.assertEqual(out.ref_peptide, "MAKW")

    def test_minus_strand_boundary_codon_matches_plus_strand_answers(self):
        # genomic bases are the complement of the CDS: c.7 -> pos 200 (T), c.8 -> pos 107 (T), c.9 -> pos 106 (T)
        cases = [
            (200, "T", "C", "K", "E", "MAEW", "missense"),  # c.7 A>G
            (107, "T", "C", "K", "R", "MARW", "missense"),  # c.8 A>G
            (106, "T", "C", "K", "K", "MAKW", "synonymous"),  # c.9 A>G
        ]
        for pos, ref, alt, ref_aa, alt_aa, alt_pep, category in cases:
            with self.subTest(pos=pos):
                out = self._run(_split_minus(), pos, ref, alt)
                self.assertIsInstance(out, VariantPeptides, out)
                self.assertEqual((out.ref_aa, out.alt_aa, out.category), (ref_aa, alt_aa, category))
                self.assertEqual(out.ref_peptide, "MAKW")
                self.assertEqual(out.alt_peptide, alt_pep)

    def test_minus_strand_ref_allele_is_checked_on_the_complement_strand(self):
        # Passing the CDS (transcript-strand) base instead of the genomic base must be refused.
        out = self._run(_split_minus(), 200, "A", "G")
        self.assertIsInstance(out, PeptideSkip)
        self.assertEqual(out.code, vp.SKIP_REF_MISMATCH)

    def test_plus_strand_ref_mismatch(self):
        out = self._run(_split_plus(), 106, "C", "G")
        self.assertEqual(out.code, vp.SKIP_REF_MISMATCH)


# ---------------------------------------------------------------------------
# Unsupported / non-coding / malformed inputs  ->  explicit skip codes, never a peptide
# ---------------------------------------------------------------------------
class TestFailClosed(unittest.TestCase):
    def setUp(self):
        self.tx = _split_plus()

    def _code(self, tx, pos, ref, alt, limit=1022):
        out = build_variant_peptides(tx, pos, ref, alt, max_residues=limit)
        self.assertIsInstance(out, PeptideSkip, out)
        return out.code

    def test_no_transcript(self):
        self.assertEqual(self._code(None, 106, "A", "G"), vp.SKIP_NO_TRANSCRIPT)

    def test_no_cds_sequence(self):
        tx = _split_plus()
        tx.cds_sequence = None
        self.assertEqual(self._code(tx, 106, "A", "G"), vp.SKIP_NO_CDS)

    def test_intronic_position(self):
        self.assertEqual(self._code(self.tx, 150, "A", "G"), vp.SKIP_NOT_IN_CDS)

    def test_utr_position_inside_an_exon(self):
        tx = TranscriptContext(
            transcript_id="ENST_UTR",
            chrom="1",
            strand=1,
            exons=[ExonSpan(90, 106, 0), ExonSpan(200, 207, 0)],
            cds_genomic_start=100,
            cds_genomic_end=207,
            protein_length=4,
            cds_sequence=_CDS_SPLIT,
        )
        self.assertEqual(self._code(tx, 95, "A", "G"), vp.SKIP_NOT_IN_CDS)

    def test_indel_and_mnv_are_not_supported(self):
        self.assertEqual(self._code(self.tx, 106, "AA", "A"), vp.SKIP_NOT_SNV)
        self.assertEqual(self._code(self.tx, 106, "A", "AGG"), vp.SKIP_NOT_SNV)
        self.assertEqual(self._code(self.tx, 106, "AA", "GG"), vp.SKIP_NOT_SNV)

    def test_symbolic_or_ambiguous_alleles(self):
        self.assertEqual(self._code(self.tx, 106, "A", "<DEL>"), vp.SKIP_NOT_SNV)
        self.assertEqual(self._code(self.tx, 106, "A", "N"), vp.SKIP_NOT_SNV)

    def test_ref_equal_alt(self):
        self.assertEqual(self._code(self.tx, 106, "A", "A"), vp.SKIP_NOT_SNV)

    def test_residue_limit_too_small(self):
        self.assertEqual(self._code(self.tx, 106, "A", "G", limit=2), vp.SKIP_BAD_LIMIT)

    def test_cds_not_multiple_of_three(self):
        tx = _single_exon("ATGGCTAAATGGTA", 1, protein_length=None)
        self.assertEqual(self._code(tx, 1001, "T", "C"), vp.SKIP_CDS_NOT_MULTIPLE_OF_3)

    def test_cds_without_start_codon(self):
        tx = _single_exon("CTGGCTAAATGGTAA", 1)
        self.assertEqual(self._code(tx, 1004, "G", "A"), vp.SKIP_CDS_NO_START)

    def test_cds_without_stop_codon(self):
        tx = _single_exon("ATGGCTAAATGGTGG", 1)
        self.assertEqual(self._code(tx, 1004, "G", "A"), vp.SKIP_CDS_NO_STOP)

    def test_cds_with_internal_stop(self):
        tx = _single_exon("ATGTAAAAATGGTAA", 1)
        self.assertEqual(self._code(tx, 1007, "A", "G"), vp.SKIP_CDS_INTERNAL_STOP)

    def test_cds_with_invalid_bases(self):
        tx = _single_exon("ATGGCTAANTGGTAA", 1)
        self.assertEqual(self._code(tx, 1004, "G", "A"), vp.SKIP_CDS_INVALID_BASES)

    def test_annotated_protein_length_must_agree_with_translation(self):
        tx = _single_exon(_CDS_SPLIT, 1, protein_length=99)
        self.assertEqual(self._code(tx, 1006, "A", "G"), vp.SKIP_PROTEIN_LENGTH_DISAGREES)

    def test_cds_length_must_agree_with_exon_arithmetic(self):
        tx = _split_plus()
        tx.cds_sequence = (
            "ATGGCTAAAAAATGGTAA"  # still a clean frame ending in a stop, but 3 nt longer than the exons support
        )
        self.assertEqual(self._code(tx, 106, "A", "G"), vp.SKIP_CDS_LENGTH_DISAGREES)


class TestStopStartAndTruncation(unittest.TestCase):
    CDS = "ATGCGAGAAGCTTAA"  # M R E A *

    def test_stop_gain_truncates_the_alternate_before_the_new_stop(self):
        tx = _single_exon(self.CDS, 1)
        pos, ref, alt = _variant_for_cds_index(tx, 3, "T")  # CGA -> TGA (Arg->stop) at codon 2, c.4
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        self.assertEqual((out.codon_number, out.ref_aa, out.alt_aa, out.category), (2, "R", "*", "nonsense"))
        self.assertEqual(out.ref_peptide, "MREA")
        self.assertEqual(out.alt_peptide, "M")
        self.assertEqual(out.variant_index, 1)
        self.assertNotIn("*", out.alt_peptide)

    def test_stop_gain_on_the_minus_strand(self):
        tx = _single_exon(self.CDS, -1)
        pos, ref, alt = _variant_for_cds_index(tx, 6, "T")  # GAA -> TAA (Glu->stop) at codon 3
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        self.assertEqual((out.codon_number, out.alt_aa, out.category), (3, "*", "nonsense"))
        self.assertEqual(out.alt_peptide, "MR")

    def test_stop_gain_at_codon_two_with_one_residue_limit_window(self):
        tx = _single_exon(self.CDS, 1)
        pos, ref, alt = _variant_for_cds_index(tx, 3, "T")
        out = build_variant_peptides(tx, pos, ref, alt, 3)
        self.assertEqual(out.alt_peptide, out.ref_peptide[: out.variant_index])

    def test_stop_loss_is_refused(self):
        tx = _single_exon(self.CDS, 1)
        pos, ref, alt = _variant_for_cds_index(tx, 12, "C")  # TAA -> CAA (stop->Gln)
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        self.assertIsInstance(out, PeptideSkip)
        self.assertEqual(out.code, vp.SKIP_STOP_LOST)

    def test_start_codon_change_is_refused(self):
        tx = _single_exon(self.CDS, 1)
        pos, ref, alt = _variant_for_cds_index(tx, 1, "C")  # ATG -> ACG
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        self.assertEqual(out.code, vp.SKIP_START_ALTERED)


# ---------------------------------------------------------------------------
# ESM-2 length limit: the window is variant-centred, never truncated away
# ---------------------------------------------------------------------------
def _long_cds(n_residues: int) -> str:
    # Cycle through four distinct amino acids so a wrong window offset shows up in the content.
    codons = ["GCT", "AAA", "GAA", "CTG"]  # A K E L
    return "ATG" + "".join(codons[i % 4] for i in range(n_residues - 1)) + "TAA"


class TestWindowLimits(unittest.TestCase):
    N = 1500

    def _case(self, codon_number, strand=1, limit=1022):
        cds = _long_cds(self.N)
        tx = _single_exon(cds, strand)
        # change the codon's middle base so the residue changes: cycle codons GCT AAA GAA CTG -> middle A->G etc.
        idx = (codon_number - 1) * 3 + 1
        new = "G" if cds[idx] != "G" else "A"
        pos, ref, alt = _variant_for_cds_index(tx, idx, new)
        protein = _translate(cds)[:-1]
        out = build_variant_peptides(tx, pos, ref, alt, limit)
        return out, protein

    def _assert_window(self, out, protein, codon_number, limit):
        self.assertIsInstance(out, VariantPeptides, out)
        self.assertLessEqual(len(out.ref_peptide), limit)
        self.assertLessEqual(len(out.alt_peptide), limit)
        self.assertEqual(out.ref_peptide, protein[out.window_start - 1 : out.window_end])
        self.assertEqual(out.window_start + out.variant_index, codon_number)
        self.assertEqual(out.ref_peptide[out.variant_index], protein[codon_number - 1])
        self.assertEqual(out.alt_peptide[out.variant_index], out.alt_aa)
        diffs = [i for i, (a, b) in enumerate(zip(out.ref_peptide, out.alt_peptide)) if a != b]
        self.assertEqual(diffs, [out.variant_index])

    def test_middle_of_a_long_protein_is_centred(self):
        out, protein = self._case(750)
        self._assert_window(out, protein, 750, 1022)
        self.assertEqual(len(out.ref_peptide), 1022)

    def test_window_clips_at_the_n_terminus(self):
        out, protein = self._case(5)
        self._assert_window(out, protein, 5, 1022)
        self.assertEqual(out.window_start, 1)

    def test_window_clips_at_the_c_terminus(self):
        out, protein = self._case(self.N - 3)
        self._assert_window(out, protein, self.N - 3, 1022)
        self.assertEqual(out.window_end, out.protein_length)
        self.assertEqual(len(out.ref_peptide), 1022)

    def test_window_on_the_minus_strand(self):
        out, protein = self._case(1100, strand=-1)
        self._assert_window(out, protein, 1100, 1022)

    def test_short_protein_is_used_whole(self):
        cds = _long_cds(100)
        tx = _single_exon(cds, 1)
        pos, ref, alt = _variant_for_cds_index(tx, 31, "G" if cds[31] != "G" else "A")
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        self.assertEqual((out.window_start, out.window_end), (1, 100))

    def test_custom_small_limit(self):
        out, protein = self._case(750, limit=101)
        self._assert_window(out, protein, 750, 101)

    def test_stop_gain_in_a_windowed_protein(self):
        cds = _long_cds(1500)
        tx = _single_exon(cds, 1)
        # find a codon we can turn into a stop with one base change: use 'GAA' (E) -> 'TAA' at codon 1203 (index pattern A K E L: codon n -> (n-2)%4)
        codon = next(n for n in range(1195, 1215) if cds[(n - 1) * 3 : (n - 1) * 3 + 3] == "GAA")
        pos, ref, alt = _variant_for_cds_index(tx, (codon - 1) * 3, "T")
        out = build_variant_peptides(tx, pos, ref, alt, 1022)
        protein = _translate(cds)[:-1]
        self.assertEqual(out.category, "nonsense")
        self.assertEqual(out.alt_peptide, protein[out.window_start - 1 : codon - 1])
        self.assertEqual(len(out.alt_peptide), out.variant_index)


# ---------------------------------------------------------------------------
# BRCA1 GRCh38 17:43093449 G>T on ENST00000357654 (real transcript record, minus strand, 23 exons)
# ---------------------------------------------------------------------------
class TestBrca1S694R(unittest.TestCase):
    POS, REF, ALT = 43093449, "G", "T"

    @classmethod
    def setUpClass(cls):
        cls.record, cls.tx = _brca1()
        cls.protein = _translate(cls.record["cds_sequence"])

    def test_fixture_is_a_clean_complete_reading_frame(self):
        cds = self.record["cds_sequence"]
        self.assertEqual(self.record["strand"], -1)
        self.assertEqual(len(cds), 5592)
        self.assertEqual(self.tx.cds_length, 5592)
        self.assertEqual(len(self.protein), 1864)  # 1863 residues + stop
        self.assertEqual(self.protein[-1], "*")
        self.assertNotIn("*", self.protein[:-1])
        self.assertTrue(cds.startswith("ATG"))
        self.assertEqual(self.record["protein_length"], 1863)
        # Published BRCA1 N-terminus (RING domain) -- checks the cached CDS is the right gene/orientation.
        self.assertTrue(
            self.protein.startswith("MDLSALRVEEVQNVINAMQKILECPICLELIKEPVSTKCDHIFCKFCMLKLLNQKKGPSQCPLCKNDITKRSLQESTRFSQ")
        )

    def test_variant_maps_to_codon_694_of_the_reading_frame(self):
        self.assertEqual(self.tx.cds_position(self.POS), 2082)  # c.2082
        codon = self.record["cds_sequence"][2079 : 2082 + 1]  # codon 694 = c.2080-2082
        self.assertEqual(self.record["cds_sequence"][2079:2082], "AGC")
        self.assertEqual(_TABLE["AGC"], "S")
        self.assertEqual(self.protein[693], "S")
        self.assertEqual(codon[:3], "AGC")

    def test_reference_and_alternate_peptides_are_correct_for_s694r(self):
        out = build_variant_peptides(self.tx, self.POS, self.REF, self.ALT, 1022)
        self.assertIsInstance(out, VariantPeptides, out)
        self.assertEqual(out.transcript_id, "ENST00000357654")
        self.assertEqual((out.codon_number, out.ref_aa, out.alt_aa, out.category), (694, "S", "R", "missense"))
        self.assertEqual(f"{out.ref_aa}{out.codon_number}{out.alt_aa}", "S694R")
        self.assertTrue(out.protein_changed)
        # 1863 residues exceeds ESM-2's 1022-residue budget, so a variant-centred window is used.
        self.assertEqual((out.window_start, out.window_end, out.variant_index), (184, 1205, 510))
        self.assertEqual(len(out.ref_peptide), 1022)
        # Reference window is exactly the transcript's own protein over that span.
        self.assertEqual(out.ref_peptide, self.protein[183:1205])
        # Alternate differs at exactly one position: Ser->Arg at the variant codon.
        diffs = [i for i, (a, b) in enumerate(zip(out.ref_peptide, out.alt_peptide)) if a != b]
        self.assertEqual(diffs, [510])
        self.assertEqual(out.ref_peptide[510], "S")
        self.assertEqual(out.alt_peptide[510], "R")
        # Residues around the variant, taken from the transcript and compared to the alternate.
        self.assertEqual(out.ref_peptide[500:521], "PNEQTSKRHDSDTFPELKLTN")
        self.assertEqual(out.alt_peptide[500:521], "PNEQTSKRHDRDTFPELKLTN")

    def test_old_flat_window_orf_cannot_be_the_input(self):
        """Run 5 regression: the old window ORF ('MYSANTEHQVHFLPFLCLFPDCG*') is unrelated to this variant."""
        out = build_variant_peptides(self.tx, self.POS, self.REF, self.ALT, 1022)
        self.assertNotIn("MYSANTEHQVHFLPFLCLFPDCG", out.alt_peptide)
        self.assertIn("R", out.alt_peptide[out.variant_index])
        self.assertNotEqual(out.ref_peptide, out.alt_peptide)

    def test_a_genuine_synonymous_change_at_the_same_codon(self):
        # G>A: genomic A -> CDS T : AGC -> AGT (Ser->Ser).
        out = build_variant_peptides(self.tx, self.POS, "G", "A", 1022)
        self.assertIsInstance(out, VariantPeptides, out)
        self.assertEqual((out.codon_number, out.ref_aa, out.alt_aa, out.category), (694, "S", "S", "synonymous"))
        self.assertFalse(out.protein_changed)
        self.assertEqual(out.ref_peptide, out.alt_peptide)
        self.assertEqual(out.ref_peptide, self.protein[183:1205])

    def test_a_ref_mismatch_at_the_real_position_is_refused(self):
        out = build_variant_peptides(self.tx, self.POS, "C", "T", 1022)
        self.assertEqual(out.code, vp.SKIP_REF_MISMATCH)

    def test_an_intronic_position_in_the_same_gene_is_refused(self):
        out = build_variant_peptides(self.tx, 43100000, "A", "G", 1022)
        self.assertEqual(out.code, vp.SKIP_NOT_IN_CDS)


# ---------------------------------------------------------------------------
# The protein stage: what ESM-2 actually receives, and how skips are represented
# ---------------------------------------------------------------------------
class _FakeESM2:
    def __init__(self):
        self.calls = []

    def predict(self, sequence):
        self.calls.append(sequence)
        return {"embedding_mean": [0.0], "embedding_dim": 1, "meta": {"sequence_length": len(sequence)}}


def _run1_style_window(variant_offset=500, alt_base="T"):
    """A 1001-nt plus-strand window shaped like Run 5's: first AUG at index 1, stop by index 73, variant far later."""
    head = "AATGTATTCTGCAAATACTGAGCATCAAGTTCACTTTCTTCCATTTCTATGCTTGTTTCC"  # Run 5's persisted 60-nt preview
    tail = "TGACTGTGGATAA"  # completes ...F P D C G *  (CCT GAC TGT GGA TAA)
    body = head + tail
    body += "GC" * ((1001 - len(body)) // 2 + 1)
    ref_seq = body[:1001]
    ref_seq = ref_seq[:variant_offset] + "G" + ref_seq[variant_offset + 1 :]
    alt_seq = ref_seq[:variant_offset] + alt_base + ref_seq[variant_offset + 1 :]
    return SimpleNamespace(ref_sequence=ref_seq, alt_sequence=alt_seq, variant_offset=variant_offset)


def _pipeline(esm2=None, legacy_context=None):
    from pipeline.orchestrator import GeperPipeline
    from pipeline.protein_translator import ProteinTranslator
    from pipeline.rna_generator import RNAGenerator

    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.router = mock.MagicMock()
    p.router.requires_protein_analysis.return_value = True
    p.rna_generator = RNAGenerator()
    p.protein_translator = ProteinTranslator()
    p._model_instances = {}
    p._model_stage_errors = {}
    return p


class TestProteinStage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record, cls.tx = _brca1()
        cls.protein = _translate(cls.record["cds_sequence"])
        cls.transcript_result = {"found": True, "transcript": cls.record}
        cls.variant = SimpleNamespace(chrom="17", pos=43093449, ref="G", alt="T")

    def _stage(self, variant=None, transcript_result="default", context=None):
        fake = _FakeESM2()
        p = _pipeline()
        errors = []
        tr = self.transcript_result if transcript_result == "default" else transcript_result
        with mock.patch.dict("pipeline.orchestrator.MODEL_REGISTRY", {"esm2": lambda: fake}):
            result = p._run_protein_stage(variant or self.variant, context or _run1_style_window(), errors, tr)
        return result, fake, errors, p

    def test_esm2_receives_the_variant_aware_alternate_window_not_the_flat_window(self):
        result, fake, errors, _ = self._stage()
        self.assertEqual(errors, [])
        # The old defect is still visible in the legacy `translation` (identical, unrelated) ...
        self.assertEqual(result["translation"]["ref_protein"], result["translation"]["alt_protein"])
        self.assertTrue(result["translation"]["alt_protein"].startswith("MYSANTEHQVHFLPFLCLF"))
        # ... but ESM-2 was given the transcript-derived alternate window.
        block = result["esm2_input"]
        self.assertEqual(block["status"], "used")
        expected_ref = self.protein[183:1205]
        expected_alt = expected_ref[:510] + "R" + expected_ref[511:]
        self.assertEqual(fake.calls, [expected_alt])
        self.assertNotEqual(fake.calls[0], result["translation"]["alt_protein"])
        self.assertEqual(block["alt_peptide"], expected_alt)
        self.assertEqual((block["codon_number"], block["ref_aa"], block["alt_aa"]), (694, "S", "R"))
        self.assertIn("esm2", result)
        self.assertFalse(result["skipped"])
        self.assertTrue(result["legacy_window_orf_found"])

    def test_the_window_orf_not_containing_the_variant_does_not_matter(self):
        # Same as above but explicit: the legacy ORF ends ~430 bases before the variant offset.
        ctx = _run1_style_window()
        stop_end = ctx.ref_sequence.index("TAA", 60) + 3
        self.assertLess(stop_end, ctx.variant_offset - 400)
        result, fake, _, _ = self._stage(context=ctx)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result["esm2_input"]["status"], "used")

    def test_no_transcript_skips_esm2_and_does_not_call_the_model(self):
        for tr in (
            None,
            {},
            {"found": False},
            {"skipped": True},
            {"found": True, "variant_outside_transcript": True, "transcript": self.record},
        ):
            with self.subTest(transcript_result=tr):
                result, fake, errors, p = self._stage(transcript_result=tr)
                self.assertEqual(fake.calls, [])
                self.assertEqual(p._model_instances, {})  # the model was not even instantiated
                self.assertNotIn("esm2", result)
                self.assertEqual(result["esm2_skip_code"], vp.SKIP_NO_TRANSCRIPT)
                self.assertEqual(result["esm2_input"]["status"], "skipped")
                self.assertEqual(result["esm2_input"]["skip_code"], vp.SKIP_NO_TRANSCRIPT)
                self.assertTrue(result["reason"])
                self.assertEqual(errors, [])

    def test_top_level_skipped_keeps_its_legacy_meaning_so_pvs1_fallback_is_unchanged(self):
        # Legacy window ORF found, ESM-2 skipped  -> skipped stays False (as before).
        result, _, _, _ = self._stage(transcript_result=None)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["translation"]["alt_protein"][:5], "MYSAN")
        # ... and PVS1's fallback still reads the legacy translation exactly as before:
        legacy_nonsense = {
            "skipped": False,
            "translation": {"ref_protein": "MKTAYIAK", "alt_protein": "MKT*"},
        }
        self.assertEqual(_protein_null_flags(legacy_nonsense), (True, False))
        # A stage result that is skipped=False with no esm2 key is processed identically by the fallback.
        legacy_nonsense_esm2_skipped = dict(legacy_nonsense, esm2_input={"status": "skipped"}, reason="x")
        self.assertEqual(_protein_null_flags(legacy_nonsense_esm2_skipped), (True, False))

    def test_no_legacy_orf_and_no_transcript_is_skipped_true_as_before(self):
        ctx = SimpleNamespace(ref_sequence="C" * 1001, alt_sequence="C" * 1001)
        result, fake, _, _ = self._stage(transcript_result=None, context=ctx)
        self.assertTrue(result["skipped"])
        self.assertEqual(fake.calls, [])
        self.assertFalse(result["legacy_window_orf_found"])

    def test_no_legacy_orf_but_valid_transcript_still_runs_esm2_and_is_not_skipped(self):
        ctx = SimpleNamespace(ref_sequence="C" * 1001, alt_sequence="C" * 1001)
        result, fake, _, _ = self._stage(context=ctx)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(result["skipped"])
        self.assertIn("esm2", result)

    def test_synonymous_variant_records_peptides_but_skips_esm2_with_protein_unchanged(self):
        syn = SimpleNamespace(chrom="17", pos=43093449, ref="G", alt="A")
        result, fake, _, p = self._stage(variant=syn)
        self.assertEqual(fake.calls, [])
        self.assertEqual(p._model_instances, {})
        self.assertNotIn("esm2", result)
        block = result["esm2_input"]
        self.assertEqual(block["status"], "skipped")
        self.assertEqual(block["skip_code"], "protein_unchanged")
        self.assertEqual(result["esm2_skip_code"], "protein_unchanged")
        self.assertEqual(block["category"], "synonymous")
        self.assertEqual(block["ref_peptide"], block["alt_peptide"])
        self.assertFalse(block["protein_changed"])
        self.assertEqual(block["ref_aa"], block["alt_aa"])

    def test_unsupported_variants_skip_with_a_code(self):
        cases = {
            "indel": SimpleNamespace(chrom="17", pos=43093449, ref="GA", alt="G"),
            "ref_mismatch": SimpleNamespace(chrom="17", pos=43093449, ref="C", alt="T"),
            "intronic": SimpleNamespace(chrom="17", pos=43100000, ref="A", alt="G"),
        }
        expected = {"indel": vp.SKIP_NOT_SNV, "ref_mismatch": vp.SKIP_REF_MISMATCH, "intronic": vp.SKIP_NOT_IN_CDS}
        for name, variant in cases.items():
            with self.subTest(case=name):
                result, fake, _, _ = self._stage(variant=variant)
                self.assertEqual(fake.calls, [])
                self.assertNotIn("esm2", result)
                self.assertEqual(result["esm2_skip_code"], expected[name])

    def test_unexpected_builder_error_fails_closed(self):
        with mock.patch("pipeline.orchestrator.build_variant_peptides", side_effect=RuntimeError("boom")):
            result, fake, errors, _ = self._stage()
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["esm2_skip_code"], vp.SKIP_CONSTRUCTION_ERROR)
        self.assertNotIn("esm2", result)

    def test_result_is_json_serialisable(self):
        result, _, _, _ = self._stage()
        json.dumps(result)

    def test_existing_model_failure_path_is_unchanged(self):
        from utils.exceptions import ModelInferenceError

        p = _pipeline()
        errors = []
        broken = mock.MagicMock()
        broken.predict.side_effect = ModelInferenceError("ESM-2 CUDA OOM")
        with mock.patch.dict("pipeline.orchestrator.MODEL_REGISTRY", {"esm2": lambda: broken}):
            result = p._run_protein_stage(self.variant, _run1_style_window(), errors, self.transcript_result)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["error"], "ESM-2 CUDA OOM")
        self.assertIn("esm2", p._model_stage_errors)


# ---------------------------------------------------------------------------
# A skipped ESM-2 must not be counted as having run
# ---------------------------------------------------------------------------
class TestSkippedEsm2IsNotCountedAsUsed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = {"found": True, "transcript": _brca1()[0]}

    def _results(self):
        fake = _FakeESM2()
        p = _pipeline()
        out = {}
        with mock.patch.dict("pipeline.orchestrator.MODEL_REGISTRY", {"esm2": lambda: fake}):
            ctx = _run1_style_window()
            out["used"] = p._run_protein_stage(SimpleNamespace(pos=43093449, ref="G", alt="T"), ctx, [], self.tr)
            out["syn"] = p._run_protein_stage(SimpleNamespace(pos=43093449, ref="G", alt="A"), ctx, [], self.tr)
            out["none"] = p._run_protein_stage(SimpleNamespace(pos=43093449, ref="G", alt="T"), ctx, [], None)
        return out

    def test_confidence_sequence_context_counts_only_a_real_esm2_run(self):
        from pipeline.confidence_engine import ConfidenceEngine

        r = self._results()
        rna = {"skipped": False, "embedding_dim": 640}
        used = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, r["used"], 1.0)
        syn = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, r["syn"], 1.0)
        none = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, r["none"], 1.0)
        self.assertIn("ESM2", used.sources_checked)
        self.assertNotIn("ESM2", syn.sources_checked)
        self.assertNotIn("ESM2", none.sources_checked)
        self.assertGreater(used.presence, syn.presence)
        self.assertEqual(syn.presence, none.presence)
        self.assertEqual(syn.sources_checked, ["HyenaDNA", "RNA-FM"])

    def test_model_status_reports_used_only_when_esm2_ran(self):
        from pipeline.models.status import USED, _esm2_status

        r = self._results()
        self.assertEqual(_esm2_status(r["used"], {"esm2": True}, {})["status"], USED)
        for key, code in (("syn", "protein_unchanged"), ("none", vp.SKIP_NO_TRANSCRIPT)):
            entry = _esm2_status(r[key], {"esm2": True}, {})
            self.assertEqual(entry["status"], "skipped", key)
            self.assertTrue(entry["reason"], key)
            self.assertEqual(r[key]["esm2_skip_code"], code)

    def test_interpretation_context_models_exclude_a_skipped_esm2(self):
        r = self._results()
        for key in ("syn", "none"):
            self.assertFalse(r[key].get("esm2"), key)  # every reader keys on this truthiness
        self.assertTrue(r["used"].get("esm2"))


if __name__ == "__main__":
    unittest.main()
