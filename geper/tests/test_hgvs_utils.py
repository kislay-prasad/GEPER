"""
Tests for pipeline/hgvs_utils.py.

No HGVS generation or validation existed anywhere in this codebase
before this module (confirmed by search: every prior "HGVS" reference
-- `pipeline/ps1_pm5/utils.py::parse_protein_change`,
`pipeline/pvs1/utils.py::termination_codon_from_hgvs_p` -- only PARSES
notation already published in a ClinVar record; none of it generates
or validates a string against reference sequence).

g. notation tests use the real, ClinVar-verified BRCA1 c.181T>G
(p.Cys61Gly) coordinate (GRCh38 chr17:43,106,487, ClinVar record 17661,
Pathogenic) already used elsewhere in this project -- IMPORTANT: BRCA1
is minus-strand (confirmed live via NCBI Gene esummary for Gene ID
672, see tests/test_hgvs_live_reference_check.py), so "c.181T>G" is in
TRANSCRIPT orientation; the genomic-forward-strand (VCF-style,
what `to_hgvs_g` takes) allele at this same real position is the
reverse complement, A>C, not T>G. `to_hgvs_g` itself is
strand-agnostic (it formats whatever ref/alt it's given), so the tests
below use the correct genomic-orientation A>C for the substitution
test and plain, non-biological ref/alt for the pure del/ins/delins
formatting tests. c./p. notation tests use a small, hand-constructed
`TranscriptContext` (two 100bp exons) chosen so the expected
CDS-relative coordinates can be verified by direct arithmetic, not
just trusted -- see each test's inline math.
"""

import unittest

from pipeline.hgvs_utils import (
    HGVSValidationResult,
    refseq_chrom_accession,
    to_hgvs_c,
    to_hgvs_g,
    to_hgvs_p,
    to_hgvs_p_for_substitution,
    validate_hgvs,
)
from pipeline.pvs1.models import ExonSpan, TranscriptContext


def _two_exon_transcript(strand: int, cds_sequence=None) -> TranscriptContext:
    """Exon 1: genomic 1000-1099 (100bp). Exon 2: genomic 2000-2099 (100bp). No UTR -- CDS spans both exons entirely."""
    return TranscriptContext(
        transcript_id="NM_999999.1",
        chrom="1",
        strand=strand,
        exons=[ExonSpan(start=1000, end=1099, rank=0), ExonSpan(start=2000, end=2099, rank=0)],
        cds_genomic_start=1000,
        cds_genomic_end=2099,
        cds_sequence=cds_sequence,
    )


# ---------------------------------------------------------------------------
# g. (genomic) notation -- real, verified BRCA1 coordinate
# ---------------------------------------------------------------------------

class TestHgvsGRealBrca1(unittest.TestCase):
    def test_real_brca1_c181t_g_substitution_in_genomic_orientation(self):
        # GRCh38 chr17:43,106,487 -- ClinVar record 17661, Pathogenic
        # (verified live against NCBI eutils elsewhere in this
        # project). Genomic-forward-strand allele is A>C (BRCA1 is
        # minus-strand -- c.181T>G is the transcript-oriented form,
        # its reverse complement; see module docstring).
        result = to_hgvs_g("17", 43106487, "A", "C", assembly="GRCh38")
        self.assertEqual(result, "NC_000017.11:g.43106487A>C")

    def test_grch37_uses_different_accession(self):
        result = to_hgvs_g("17", 100, "A", "G", assembly="GRCh37")
        self.assertTrue(result.startswith("NC_000017.10:g."))

    def test_unknown_assembly_returns_none(self):
        self.assertIsNone(to_hgvs_g("17", 100, "A", "G", assembly="hg99"))

    def test_chr_prefix_stripped(self):
        self.assertEqual(refseq_chrom_accession("chr17", "GRCh38"), "NC_000017.11")
        self.assertEqual(refseq_chrom_accession("17", "GRCh38"), "NC_000017.11")


class TestHgvsGDelInsForms(unittest.TestCase):
    def test_deletion(self):
        # VCF-anchored: ref="AA" (anchor 'A' + deleted 'A'), alt="A".
        self.assertEqual(to_hgvs_g("17", 100, "AA", "A"), "NC_000017.11:g.101del")

    def test_multi_base_deletion(self):
        self.assertEqual(to_hgvs_g("17", 100, "ATCG", "A"), "NC_000017.11:g.101_103del")

    def test_insertion(self):
        self.assertEqual(to_hgvs_g("17", 100, "A", "ATG"), "NC_000017.11:g.100_101insTG")

    def test_delins(self):
        self.assertEqual(to_hgvs_g("17", 100, "AT", "GC"), "NC_000017.11:g.100_101delinsGC")


# ---------------------------------------------------------------------------
# c. (coding) notation -- hand-verifiable synthetic transcript
# ---------------------------------------------------------------------------

class TestHgvsCPlusStrand(unittest.TestCase):
    def test_substitution_in_first_exon(self):
        tc = _two_exon_transcript(strand=1)
        # genomic 1050 is the 51st base of exon 1 (1050 - 1000 + 1 = 51) -> cds position 51.
        result = to_hgvs_c(tc, 1050, "A", "G")
        self.assertEqual(result, "NM_999999.1:c.51A>G")

    def test_substitution_at_exon_boundary_crossing_into_second_exon_cds_numbering(self):
        # genomic 2000 is the first base of exon 2 -> cds position 101 (100 bases of exon 1 + 1).
        result = to_hgvs_c(tc := _two_exon_transcript(strand=1), 2000, "C", "T")
        self.assertEqual(result, "NM_999999.1:c.101C>T")

    def test_intronic_position_returns_none(self):
        tc = _two_exon_transcript(strand=1)
        # genomic 1500 falls in the intron between the two exons.
        self.assertIsNone(to_hgvs_c(tc, 1500, "A", "G"))


class TestHgvsCMinusStrand(unittest.TestCase):
    def test_substitution_reverse_complemented_to_transcript_orientation(self):
        tc = _two_exon_transcript(strand=-1)
        # cds_position(2050) on minus strand = span.end(2099) - 2050 + span.cds_start(1) = 50.
        # ref/alt are genomic-forward-strand bases (A/G) -> transcript orientation is their reverse complement (T/C).
        result = to_hgvs_c(tc, 2050, "A", "G")
        self.assertEqual(result, "NM_999999.1:c.50T>C")


# ---------------------------------------------------------------------------
# p. (protein) notation
# ---------------------------------------------------------------------------

class TestHgvsP(unittest.TestCase):
    # 5 codons: ATG(Met/start) AAA(Lys) CCC(Pro) TTT(Phe) TAA(Stop).
    _CDS = "ATGAAACCCTTTTAA"

    def test_reference_codon_translation(self):
        tc = _two_exon_transcript(strand=1, cds_sequence=self._CDS)
        result = to_hgvs_p(tc, codon_number=2)  # AAA = Lys
        self.assertEqual(result, "p.(Lys2=)")

    def test_missense_substitution(self):
        tc = _two_exon_transcript(strand=1, cds_sequence=self._CDS)
        alt_cds = "ATGAGACCCTTTTAA"  # codon 2: AAA -> AGA (Lys -> Arg)
        result = to_hgvs_p_for_substitution(tc, codon_number=2, alt_cds_sequence=alt_cds)
        self.assertEqual(result, "p.(Lys2Arg)")

    def test_nonsense_substitution(self):
        tc = _two_exon_transcript(strand=1, cds_sequence=self._CDS)
        alt_cds = "ATGTAACCCTTTTAA"  # codon 2: AAA -> TAA (Lys -> Stop)
        result = to_hgvs_p_for_substitution(tc, codon_number=2, alt_cds_sequence=alt_cds)
        self.assertEqual(result, "p.(Lys2Ter)")

    def test_no_cds_sequence_returns_none_not_fabricated(self):
        tc = _two_exon_transcript(strand=1, cds_sequence=None)
        self.assertIsNone(to_hgvs_p(tc, codon_number=2))

    def test_codon_number_out_of_range_returns_none(self):
        tc = _two_exon_transcript(strand=1, cds_sequence=self._CDS)
        self.assertIsNone(to_hgvs_p(tc, codon_number=999))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateHgvsSyntax(unittest.TestCase):
    def test_well_formed_substitution_passes_without_fetcher(self):
        result = validate_hgvs("NC_000017.11:g.43106487T>G")
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.kind, "g")
        self.assertTrue(any("not cross-checked" in w for w in result.warnings))

    def test_malformed_shape_fails(self):
        result = validate_hgvs("this is not hgvs at all")
        self.assertFalse(result.is_valid)
        self.assertIn("does not match the expected HGVS shape", result.errors[0])

    def test_missing_accession_version_fails(self):
        # 'NM_007294' with no '.4' version suffix -- a common real-world mistake.
        result = validate_hgvs("NM_007294:c.181T>G")
        self.assertFalse(result.is_valid)
        self.assertIn("not a valid RefSeq accession", result.errors[0])

    def test_wrong_accession_prefix_for_kind_fails(self):
        # NM_ (mRNA) accession used with g. (genomic) notation -- mismatched kind/prefix.
        result = validate_hgvs("NM_007294.4:g.181T>G")
        self.assertFalse(result.is_valid)

    def test_malformed_transcript_id_fails(self):
        result = validate_hgvs("ENST00000357654:c.181T>G")  # Ensembl-style ID, not RefSeq
        self.assertFalse(result.is_valid)

    def test_deletion_insertion_delins_syntax_recognized(self):
        for hgvs in ["NC_000017.11:g.101del", "NC_000017.11:g.101_103del", "NC_000017.11:g.100_101insTG", "NC_000017.11:g.100_101delinsGC"]:
            with self.subTest(hgvs=hgvs):
                result = validate_hgvs(hgvs)
                self.assertTrue(result.is_valid, msg=result.errors)

    def test_unrecognized_description_form_fails(self):
        result = validate_hgvs("NC_000017.11:g.this_is_garbage")
        self.assertFalse(result.is_valid)


class TestValidateHgvsReferenceCrossCheck(unittest.TestCase):
    def _fetch_base(self, chrom: str, pos: int) -> str:
        # Genomic-forward-strand base at BRCA1's real chr17:43,106,487
        # is 'A' (BRCA1 is minus-strand; see module docstring and
        # tests/test_hgvs_live_reference_check.py's live confirmation).
        real_genome = {43106487: "A"}
        return real_genome.get(pos, "N")

    def test_correct_reference_base_passes(self):
        result = validate_hgvs("NC_000017.11:g.43106487A>C", fetch_reference_base=self._fetch_base)
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.warnings, [])

    def test_wrong_reference_base_fails_with_clear_message(self):
        # 'T' is the transcript-oriented ref (c.181T>G) -- wrong if
        # asserted as the genomic g. reference base, exactly the
        # strand-confusion mistake this cross-check exists to catch.
        result = validate_hgvs("NC_000017.11:g.43106487T>G", fetch_reference_base=self._fetch_base)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("states reference base 'T'" in e and "real reference genome has 'A'" in e for e in result.errors), msg=result.errors)

    def test_no_fetcher_warns_rather_than_fails(self):
        result = validate_hgvs("NC_000017.11:g.43106487A>C", fetch_reference_base=None)
        self.assertTrue(result.is_valid)
        self.assertTrue(any("No reference sequence source" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
