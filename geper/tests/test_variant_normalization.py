"""
Tests for pipeline/variant_normalization.py.

No normalization logic of any kind existed anywhere in this codebase
before this module (confirmed by search: `pipeline/vcf_parser.py`
already splits multi-allelic sites into one Variant per ALT allele --
real, pre-existing, not re-tested here -- but never trims or
left-aligns). Every fixture below is hand-constructed to the real,
published normalization algorithm (Tan et al. 2015, "Unified
representation of genetic variants," Bioinformatics -- the same
algorithm bcftools norm / vt normalize implement), with reference
sequences chosen to make the expected result independently verifiable
by inspection (e.g. a plain homopolymer run), not opaque real genome
coordinates.
"""

import unittest

from pipeline.variant_normalization import bare_spdi, normalize_variant


class TestParsimonyTrimOnly(unittest.TestCase):
    """No fetch_base supplied -- reference-free trimming only."""

    def test_padded_substitution_reduces_to_minimal_snv(self):
        # ATG -> ACG is really just T>C at pos+1, with A/G padding on both sides.
        r = normalize_variant("1", 100, "ATG", "ACG")
        self.assertEqual((r.pos, r.ref, r.alt), (101, "T", "C"))
        self.assertTrue(r.was_trimmed)
        self.assertTrue(r.changed)

    def test_already_minimal_snv_is_unchanged(self):
        r = normalize_variant("1", 100, "A", "G")
        self.assertEqual((r.pos, r.ref, r.alt), (100, "A", "G"))
        self.assertFalse(r.changed)

    def test_common_suffix_trimmed_first(self):
        # CAG -> CAAG (insertion of 'A'): common suffix "AG" trimmed first, then no more common prefix/suffix remains.
        r = normalize_variant("1", 100, "CAG", "CAAG")
        self.assertEqual((r.pos, r.ref, r.alt), (100, "C", "CA"))
        self.assertTrue(r.was_trimmed)

    def test_indel_without_reference_source_is_trimmed_but_not_left_aligned(self):
        r = normalize_variant("1", 103, "AA", "A")  # no fetch_base given
        self.assertEqual(
            (r.pos, r.ref, r.alt), (103, "AA", "A")
        )  # already minimal VCF-anchored form; nothing further to trim
        self.assertFalse(r.was_left_aligned)
        self.assertIn("no reference sequence source", r.left_align_skipped_reason)

    def test_monomorphic_call_passed_through_unchanged(self):
        r = normalize_variant("1", 100, "A", "A")
        self.assertEqual((r.pos, r.ref, r.alt), (100, "A", "A"))
        self.assertFalse(r.changed)


class TestLeftAlignment(unittest.TestCase):
    """
    Reference sequence around positions 100-106: G(100) A(101) A(102)
    A(103) A(104) T(105) C(106) -- a 4-base homopolymer run of 'A' at
    101-104, the classic case left-alignment exists for.
    """

    _REF = {100: "G", 101: "A", 102: "A", 103: "A", 104: "A", 105: "T", 106: "C"}

    def _fetch_base(self, chrom: str, pos: int) -> str:
        return self._REF[pos]

    def test_deletion_left_aligns_to_leftmost_anchor_in_homopolymer(self):
        # "Delete one A", anchored at 103 (deleting the A at 104) -- should shift left to the true leftmost anchor, 101.
        r = normalize_variant("1", 103, "AA", "A", fetch_base=self._fetch_base)
        self.assertEqual((r.pos, r.ref, r.alt), (101, "AA", "A"))
        self.assertTrue(r.was_left_aligned)
        self.assertTrue(r.changed)

    def test_deletion_already_at_leftmost_anchor_is_unchanged(self):
        r = normalize_variant("1", 101, "AA", "A", fetch_base=self._fetch_base)
        self.assertEqual((r.pos, r.ref, r.alt), (101, "AA", "A"))
        # Position 100 is 'G', not 'A' -- shifting further would change the variant, so no shift happens.
        self.assertFalse(r.was_left_aligned)

    def test_insertion_left_aligns_to_leftmost_anchor_in_homopolymer(self):
        # Inserting one extra 'A' into the run, anchored at 104 -- should shift left to 101.
        r = normalize_variant("1", 104, "A", "AA", fetch_base=self._fetch_base)
        self.assertEqual((r.pos, r.ref, r.alt), (101, "A", "AA"))
        self.assertTrue(r.was_left_aligned)

    def test_deletion_outside_the_repeat_run_does_not_shift(self):
        # Deleting the 'T' at 105 (anchored at 104, deleting 105) -- position 104 is part of the run but the base being dropped ('T') doesn't match position 103's 'A', so no shift.
        r = normalize_variant("1", 104, "AT", "A", fetch_base=self._fetch_base)
        self.assertEqual((r.pos, r.ref, r.alt), (104, "AT", "A"))
        self.assertFalse(r.was_left_aligned)

    def test_stops_at_max_shift_bound(self):
        # A pathologically long run: every position 1..1000 is 'A'. With
        # a max_shift_bp of 3, left-alignment must stop after 3 shifts
        # rather than walking the whole (in production, real) genome.
        long_run = {i: "A" for i in range(1, 1001)}
        r = normalize_variant("1", 500, "AA", "A", fetch_base=lambda c, p: long_run[p], max_shift_bp=3)
        self.assertEqual(r.pos, 497)  # exactly 3 shifts from 500
        self.assertTrue(r.was_left_aligned)


class TestComplexDelinsNotLeftAligned(unittest.TestCase):
    def test_complex_delins_is_trimmed_but_not_left_aligned(self):
        # ref/alt share no prefix/suffix relationship after trimming (not a clean indel) -- left-alignment correctly does not apply.
        def fetch_base(chrom, pos):
            return "A"  # would match if left-align were (incorrectly) attempted

        r = normalize_variant("1", 100, "AT", "CG", fetch_base=fetch_base)
        self.assertEqual((r.pos, r.ref, r.alt), (100, "AT", "CG"))
        self.assertFalse(r.was_left_aligned)


class TestAlreadyMinimalSnvIsUnaffectedByOrientation(unittest.TestCase):
    """
    `normalize_variant` is a pure string/position operation -- it does
    not know or care about gene strand, so this passes regardless of
    which orientation a caller's ref/alt happen to be in. (See
    tests/test_hgvs_utils.py's module docstring for why BRCA1's real
    genomic-forward-strand allele at this position is A>C, not the
    T>G shown here -- irrelevant to what this test actually checks:
    that an already-minimal single-base substitution is left alone.)
    """

    def test_already_minimal_snv_passes_through_unchanged(self):
        r = normalize_variant("17", 43106487, "T", "G")
        self.assertEqual((r.pos, r.ref, r.alt), (43106487, "T", "G"))
        self.assertFalse(r.changed)


class TestBareSpdi(unittest.TestCase):
    """
    `bare_spdi` (added for the I1 ClinVar/dbSNP indel-matching fix --
    see `database/clinvar_client.py::ClinVarClient._variant_match`)
    trims all the way to empty, unlike `trim_variant` which always
    keeps a >=1-base VCF anchor.
    """

    def test_insertion_reduces_to_empty_ref(self):
        # Real VHL c.422dup case: query variant 10146594 A>AA.
        self.assertEqual(bare_spdi(10146594, "A", "AA"), (10146594, "", "A"))

    def test_non_minimal_spdi_window_reduces_to_same_bare_form(self):
        # ClinVar's own (non-minimal) canonical_spdi for the same variant.
        self.assertEqual(bare_spdi(10146594, "AA", "AAA"), (10146594, "", "A"))

    def test_deletion_reduces_to_empty_alt(self):
        # Real BRCA1 c.1232_1233del case: VCF-anchored 43094297 CAT>C.
        self.assertEqual(bare_spdi(43094297, "CAT", "C"), (43094298, "AT", ""))

    def test_already_bare_deletion_is_unchanged(self):
        # ClinVar's own canonical_spdi for the same deletion.
        self.assertEqual(bare_spdi(43094298, "AT", ""), (43094298, "AT", ""))

    def test_snv_is_unaffected(self):
        self.assertEqual(bare_spdi(100, "A", "C"), (100, "A", "C"))

    def test_no_shared_prefix_or_suffix_leaves_indel_unchanged(self):
        self.assertEqual(bare_spdi(100, "GC", "T"), (100, "GC", "T"))


if __name__ == "__main__":
    unittest.main()
