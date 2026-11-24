"""
Regression tests for ClinVar audit finding C1: `pipeline/variant_normalization.py` left-alignment
turned some indels into DIFFERENT variants.

Root cause (see the module docstring there): the shift test compared the last deleted/inserted base
with the reference base BEFORE THE ANCHOR instead of with the other allele's last base. A shift is
sequence-preserving iff `ref[-1] == alt[-1]`. In a homopolymer every base is equal, so the old test
looked right; in any other repeat it produced a different variant.

Three layers, none of which relies on homopolymer examples alone:

1. Real data. `fixtures/c1_clinvar_indel_reference_windows.json` freezes 80 real BRCA1/BRCA2 ClinVar
   indels (the nine the audit found corrupted plus 71 more), each with its real ClinVar
   `canonical_spdi`/`variation_loc` and the real +/-250 bp reference window on BOTH GRCh38 and GRCh37
   (Ensembl REST, fetched through GEPER's own `SequenceContextGenerator`). Everything below runs
   offline against it.
2. A property test against an independent implementation of Tan et al. 2015 on random repeat-rich
   references.
3. Behavioural tests for REF-allele validation, reference outages, window fetching, the shift bound,
   multiallelic records and the orchestrator warnings.
"""

import json
import os
import random
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from database.clinvar_client import ClinVarClient
from pipeline.variant_normalization import normalize_variant
from pipeline.vcf_parser import Variant, VCFParser
from utils.exceptions import ExternalAPIError

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "c1_clinvar_indel_reference_windows.json")
with open(_FIXTURE, "r", encoding="utf-8") as _fh:
    _DOC = json.load(_fh)
_RECORDS = _DOC["records"]
_NINE = _DOC["failing_nine"]

# Expected GRCh38 output for the nine records the audit found corrupted, as (pos, ref, alt). Eight are
# already left-aligned in the caller's form and must come back unchanged; the ninth (VCV005042243) is a
# CA/AC-rotation insertion that shifts one base.
_NINE_EXPECTED_GRCH38 = {
    "VCV005042243": (43091716, "T", "TTG"),
    "VCV005033197": (43071021, "AC", "A"),
    "VCV005021116": (43124007, "GCTGACT", "G"),
    "VCV005019327": (43051098, "A", "AG"),
    "VCV004883931": (43063867, "TA", "T"),
    "VCV004882953": (43094803, "TTA", "T"),
    "VCV005033330": (32337477, "G", "GA"),
    "VCV005033240": (32379758, "AGTATTTGG", "A"),
    "VCV004919673": (32340235, "TAG", "T"),
}

# Records whose ALREADY left-aligned (canonical) form the old code changed into a different variant.
# Found while building this fix: the audit's input form was SPDI-derived and rarely canonical, so it
# understated the problem. 22 of the 80 canonical forms were corrupted (nine of them are the audit's).
_OLD_CORRUPTED_CANONICAL = [
    "VCV004867698", "VCV004876034", "VCV004876080", "VCV004882953", "VCV004883931", "VCV004919673",
    "VCV005005241", "VCV005019327", "VCV005021116", "VCV005021272", "VCV005033197", "VCV005033212",
    "VCV005033233", "VCV005033240", "VCV005033264", "VCV005033303", "VCV005033330", "VCV005042210",
    "VCV005042243", "VCV005042298", "VCV005042465", "VCV005042610",
]  # fmt: skip

_BUILDS = ("GRCh38", "GRCh37")


def _apply(sequence, start, pos, ref, alt):
    """The sequence that results from applying (pos, ref, alt) to `sequence` (1-based, `start` = its first position)."""
    offset = pos - start
    assert sequence[offset : offset + len(ref)] == ref, "REF does not match the reference window"
    return sequence[:offset] + alt + sequence[offset + len(ref) :]


def _window_source(sequence, start, counter=None):
    """A `fetch_sequence` over one frozen reference window; reading outside it raises, like a failed lookup."""

    def fetch_sequence(chrom, lo, hi):
        if counter is not None:
            counter.append((lo, hi))
        if lo < start or hi >= start + len(sequence):
            raise IndexError(f"{chrom}:{lo}-{hi} is outside the frozen window")
        return sequence[lo - start : hi - start + 1]

    return fetch_sequence


def _record_source(record, build, counter=None):
    b = record[build]
    return _window_source(b["window"], b["window_start"], counter)


def _tan_normalize(sequence, start, pos, ref, alt):
    """
    An independent, textbook implementation of Tan et al. 2015 (right-trim, extend left when an allele
    empties, then left-trim), written with empty alleles and no anchor bookkeeping, used as the oracle.
    """
    while True:
        if ref and alt and ref[-1] == alt[-1] and (pos > 1 or (len(ref) > 1 and len(alt) > 1)):
            ref, alt = ref[:-1], alt[:-1]
        elif (not ref or not alt) and pos > 1:
            base = sequence[pos - 1 - start]
            ref, alt, pos = base + ref, base + alt, pos - 1
        else:
            break
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref, alt, pos = ref[1:], alt[1:], pos + 1
    return pos, ref, alt


def _old_left_align(pos, ref, alt, base_at):
    """A literal copy of the DEFECTIVE shift loop (compares the last deleted/inserted base with the base before the anchor)."""
    is_deletion = len(ref) > len(alt) and ref.startswith(alt)
    is_insertion = len(alt) > len(ref) and alt.startswith(ref)
    if not (is_deletion or is_insertion):
        return pos, ref, alt
    while pos > 1:
        compare_char = ref[-1] if is_deletion else alt[-1]
        prev_base = base_at(pos - 1)
        if prev_base != compare_char:
            break
        if is_deletion:
            ref, alt = prev_base + ref[:-1], prev_base
        else:
            alt, ref = prev_base + alt[:-1], prev_base
        pos -= 1
    return pos, ref, alt


def _clinvar_entry(record):
    return {"variation_set": [{"canonical_spdi": record["canonical_spdi"], "variation_loc": record["variation_loc"]}]}


def _matches_own_record(record, build, pos, ref, alt):
    variant = Variant(chrom=record["chrom"], pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)
    return ClinVarClient._variant_match(_clinvar_entry(record), variant, build)


# ---------------------------------------------------------------------------------------------------
# 1. The exact variants that failed
# ---------------------------------------------------------------------------------------------------


class TestTheNineAuditedFailures(unittest.TestCase):
    def test_all_nine_are_in_the_fixture(self):
        self.assertEqual(set(_NINE), set(_NINE_EXPECTED_GRCH38))
        self.assertTrue(set(_NINE) <= {r["accession"] for r in _RECORDS})

    def test_each_normalises_to_the_expected_equivalent_variant_and_matches_its_own_clinvar_record(self):
        by_accession = {r["accession"]: r for r in _RECORDS}
        for accession in _NINE:
            with self.subTest(accession=accession):
                record = by_accession[accession]
                b = record["GRCh38"]
                result = normalize_variant(
                    record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=_record_source(record, "GRCh38")
                )
                self.assertEqual((result.pos, result.ref, result.alt), _NINE_EXPECTED_GRCH38[accession])
                self.assertEqual(result.reference_check, "matched")
                self.assertEqual(
                    _apply(b["window"], b["window_start"], result.pos, result.ref, result.alt),
                    _apply(b["window"], b["window_start"], b["pos"], b["ref"], b["alt"]),
                    "the normalised variant must be the same sequence change as the input",
                )
                self.assertTrue(_matches_own_record(record, "GRCh38", result.pos, result.ref, result.alt))

    def test_the_fixture_really_exercises_the_old_defect(self):
        # Guard against a fixture that has lost its teeth: the defective rule must still turn the canonical
        # form of every one of the nine into a DIFFERENT sequence change.
        by_accession = {r["accession"]: r for r in _RECORDS}
        for accession in _NINE:
            with self.subTest(accession=accession):
                b = by_accession[accession]["GRCh38"]
                window, start = b["window"], b["window_start"]
                pos, ref, alt = _tan_normalize(window, start, b["pos"], b["ref"], b["alt"])
                old = _old_left_align(pos, ref, alt, lambda p: window[p - start])
                self.assertNotEqual(old, (pos, ref, alt))
                self.assertNotEqual(_apply(window, start, *old), _apply(window, start, pos, ref, alt))


# ---------------------------------------------------------------------------------------------------
# 2. All 80 records, both builds
# ---------------------------------------------------------------------------------------------------


class TestEightyRealClinvarIndels(unittest.TestCase):
    def test_fixture_shape(self):
        self.assertEqual(len(_RECORDS), 80)
        self.assertTrue(all("GRCh37" in r and "GRCh38" in r for r in _RECORDS))

    def test_every_normalised_variant_is_sequence_equivalent_and_checked_against_the_reference(self):
        for build in _BUILDS:
            for record in _RECORDS:
                with self.subTest(build=build, accession=record["accession"]):
                    b = record[build]
                    result = normalize_variant(
                        record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=_record_source(record, build)
                    )
                    self.assertEqual(result.reference_check, "matched")
                    self.assertEqual(
                        _apply(b["window"], b["window_start"], result.pos, result.ref, result.alt),
                        _apply(b["window"], b["window_start"], b["pos"], b["ref"], b["alt"]),
                    )

    def test_clinvar_to_vcf_round_trip_preserves_identity(self):
        for build in _BUILDS:
            for record in _RECORDS:
                with self.subTest(build=build, accession=record["accession"]):
                    b = record[build]
                    result = normalize_variant(
                        record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=_record_source(record, build)
                    )
                    self.assertTrue(
                        _matches_own_record(record, build, result.pos, result.ref, result.alt),
                        f"{record['accession']} ({build}) no longer matches its own ClinVar record",
                    )

    def test_already_normalised_caller_input_is_never_changed(self):
        # The canonical form of every record is a fixed point (idempotent), on both builds.
        for build in _BUILDS:
            for record in _RECORDS:
                with self.subTest(build=build, accession=record["accession"]):
                    b = record[build]
                    source = _record_source(record, build)
                    canonical = normalize_variant(record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=source)
                    again = normalize_variant(
                        record["chrom"], canonical.pos, canonical.ref, canonical.alt, fetch_sequence=source
                    )
                    self.assertEqual((again.pos, again.ref, again.alt), (canonical.pos, canonical.ref, canonical.alt))
                    self.assertFalse(again.changed)

    def test_the_twenty_two_canonical_inputs_the_old_code_corrupted_are_left_alone(self):
        by_accession = {r["accession"]: r for r in _RECORDS}
        self.assertEqual(len(_OLD_CORRUPTED_CANONICAL), 22)
        for accession in _OLD_CORRUPTED_CANONICAL:
            for build in _BUILDS:
                with self.subTest(accession=accession, build=build):
                    record = by_accession[accession]
                    b = record[build]
                    source = _record_source(record, build)
                    pos, ref, alt = _tan_normalize(b["window"], b["window_start"], b["pos"], b["ref"], b["alt"])
                    result = normalize_variant(record["chrom"], pos, ref, alt, fetch_sequence=source)
                    self.assertEqual((result.pos, result.ref, result.alt), (pos, ref, alt))
                    self.assertFalse(result.was_left_aligned)

    def test_output_equals_the_independent_tan_oracle_for_every_record(self):
        for build in _BUILDS:
            for record in _RECORDS:
                with self.subTest(build=build, accession=record["accession"]):
                    b = record[build]
                    result = normalize_variant(
                        record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=_record_source(record, build)
                    )
                    self.assertEqual(
                        (result.pos, result.ref, result.alt),
                        _tan_normalize(b["window"], b["window_start"], b["pos"], b["ref"], b["alt"]),
                    )

    def test_grch37_and_grch38_agree_on_the_variant(self):
        # Same locus, different build coordinates: after normalisation the allele pair is the same.
        for record in _RECORDS:
            with self.subTest(accession=record["accession"]):
                out = {}
                for build in _BUILDS:
                    b = record[build]
                    r = normalize_variant(
                        record["chrom"], b["pos"], b["ref"], b["alt"], fetch_sequence=_record_source(record, build)
                    )
                    out[build] = (r.ref, r.alt)
                self.assertEqual(out["GRCh38"], out["GRCh37"])


# ---------------------------------------------------------------------------------------------------
# 3. Repeats of every period, hand-built so the expectation is checkable by eye
# ---------------------------------------------------------------------------------------------------


class TestRepeatRegions(unittest.TestCase):
    def _run(self, reference, pos, ref, alt, **kwargs):
        return normalize_variant("1", pos, ref, alt, fetch_sequence=_window_source(reference, 1), **kwargs)

    def test_non_homopolymer_deletion_whose_anchor_differs_from_the_last_deleted_base_is_not_shifted(self):
        # C G T A G T A G C : deleting 'AG' at 4-5 anchored on T@3. The base before the anchor (G@2) equals
        # the last deleted base (G) -- the old rule shifted to (2, GTA, G), which deletes T@3,A@4 instead.
        reference = "CGTAGTAGC"
        result = self._run(reference, 3, "TAG", "T")
        self.assertEqual((result.pos, result.ref, result.alt), (3, "TAG", "T"))
        self.assertFalse(result.was_left_aligned)
        self.assertNotEqual(_apply(reference, 1, 2, "GTA", "G"), _apply(reference, 1, 3, "TAG", "T"))

    def test_dinucleotide_repeat_deletion_shifts_to_the_true_leftmost_anchor(self):
        # G C A C A C A T : delete 'CA' at 6-7 anchored on A@5 -> leftmost is anchor G@1 deleting C@2,A@3.
        reference = "GCACACAT"
        result = self._run(reference, 5, "ACA", "A")
        self.assertEqual((result.pos, result.ref, result.alt), (1, "GCA", "G"))
        self.assertTrue(result.was_left_aligned)
        self.assertEqual(_apply(reference, 1, 1, "GCA", "G"), _apply(reference, 1, 5, "ACA", "A"))

    def test_trinucleotide_repeat_duplication(self):
        # T AGC AGC AGC T : duplicate one AGC (insert AGC after the last repeat unit).
        reference = "TAGCAGCAGCTT"
        result = self._run(reference, 10, "C", "CAGC")  # inserted after C@10
        self.assertEqual(
            _apply(reference, 1, result.pos, result.ref, result.alt), _apply(reference, 1, 10, "C", "CAGC")
        )
        self.assertEqual((result.pos, result.ref, result.alt), (1, "T", "TAGC"))

    def test_single_base_duplication_in_a_homopolymer_anchors_on_the_base_before_the_run(self):
        reference = "GAAAAT"
        result = self._run(reference, 5, "A", "AA")
        self.assertEqual((result.pos, result.ref, result.alt), (1, "G", "GA"))

    def test_variant_at_position_one_never_gets_an_empty_allele(self):
        reference = "AAAC"
        result = self._run(reference, 1, "AA", "A")
        self.assertEqual((result.pos, result.ref, result.alt), (1, "AA", "A"))

    def test_complex_delins_is_left_unchanged_and_equivalent(self):
        reference = "GACGTACGT"
        result = self._run(reference, 2, "ACG", "TT")
        self.assertEqual((result.pos, result.ref, result.alt), (2, "ACG", "TT"))
        self.assertFalse(result.was_left_aligned)

    def test_complex_delins_that_trims_to_a_pure_indel_is_then_left_aligned_correctly(self):
        # ACGT -> ACGTCGT is, after trimming, an insertion of CGT; it must stay the same sequence change.
        reference = "TTACGTACGTT"
        result = self._run(reference, 3, "ACGT", "ACGTCGT")
        self.assertEqual(
            _apply(reference, 1, result.pos, result.ref, result.alt), _apply(reference, 1, 3, "ACGT", "ACGTCGT")
        )

    def test_max_shift_bound_stops_early_but_stays_equivalent_and_says_so(self):
        reference = "G" + "A" * 60 + "T"
        result = self._run(reference, 50, "AA", "A", max_shift_bp=5)
        self.assertEqual(_apply(reference, 1, result.pos, result.ref, result.alt), _apply(reference, 1, 50, "AA", "A"))
        self.assertEqual(result.pos, 45)
        self.assertIn("shift limit", result.left_align_skipped_reason)

    def test_long_run_is_read_in_windows_not_base_by_base(self):
        reference = "G" + "A" * 150 + "T"
        calls = []
        result = normalize_variant(
            "1", 140, "AA", "A", fetch_sequence=_window_source(reference, 1, calls), max_shift_bp=200
        )
        self.assertEqual((result.pos, result.ref, result.alt), (1, "GA", "G"))
        self.assertLessEqual(len(calls), 6, calls)  # one REF check plus a few 64-bp chunks, not ~140 requests


# ---------------------------------------------------------------------------------------------------
# 4. Property test against the independent oracle
# ---------------------------------------------------------------------------------------------------


def _random_repeat_reference(rng):
    pieces = []
    for _ in range(rng.randint(4, 7)):
        unit = "".join(rng.choice("ACGT") for _ in range(rng.randint(1, 4)))
        pieces.append(unit * rng.randint(1, 4))
        pieces.append("".join(rng.choice("ACGT") for _ in range(rng.randint(0, 3))))
    return "".join(pieces)


class TestPropertyOnRandomRepeatReferences(unittest.TestCase):
    def test_equivalent_idempotent_leftmost_and_equal_to_the_oracle(self):
        rng = random.Random(20261124)
        checked = 0
        for _ in range(60):
            reference = _random_repeat_reference(rng)
            n = len(reference)
            source = _window_source(reference, 1)
            for pos in range(1, n):
                for length in (1, 2, 3, 4):
                    candidates = []
                    if pos + length <= n:  # deletion with the VCF anchor at pos
                        candidates.append((pos, reference[pos - 1 : pos + length], reference[pos - 1]))
                    inserted = "".join(rng.choice("ACGT") for _ in range(length))
                    candidates.append((pos, reference[pos - 1], reference[pos - 1] + inserted))
                    for p, ref, alt in candidates:
                        result = normalize_variant("1", p, ref, alt, fetch_sequence=source)
                        original = _apply(reference, 1, p, ref, alt)
                        self.assertEqual(
                            _apply(reference, 1, result.pos, result.ref, result.alt), original, (reference, p, ref, alt)
                        )
                        self.assertEqual(
                            (result.pos, result.ref, result.alt), _tan_normalize(reference, 1, p, ref, alt)
                        )
                        again = normalize_variant("1", result.pos, result.ref, result.alt, fetch_sequence=source)
                        self.assertEqual((again.pos, again.ref, again.alt), (result.pos, result.ref, result.alt))
                        if result.pos > 1:  # leftmost: the one-base-left shift must describe a different sequence
                            prev = reference[result.pos - 2]
                            shifted = (result.pos - 1, prev + result.ref[:-1], prev + result.alt[:-1])
                            self.assertNotEqual(_apply(reference, 1, *shifted), original, (reference, p, ref, alt))
                        checked += 1
        self.assertGreater(checked, 3000)

    def test_complex_delins_and_mnvs_stay_equivalent(self):
        rng = random.Random(7)
        for _ in range(40):
            reference = _random_repeat_reference(rng)
            source = _window_source(reference, 1)
            for pos in range(1, len(reference) - 3):
                ref = reference[pos - 1 : pos - 1 + rng.randint(1, 4)]
                alt = "".join(rng.choice("ACGT") for _ in range(rng.randint(1, 4)))
                if ref == alt:
                    continue
                result = normalize_variant("1", pos, ref, alt, fetch_sequence=source)
                self.assertEqual(
                    _apply(reference, 1, result.pos, result.ref, result.alt), _apply(reference, 1, pos, ref, alt)
                )


# ---------------------------------------------------------------------------------------------------
# 5. REF validation, outages, legacy callers
# ---------------------------------------------------------------------------------------------------


class TestReferenceAlleleValidation(unittest.TestCase):
    REFERENCE = "CCGTAGTAGTAGCC"

    def _source(self):
        return _window_source(self.REFERENCE, 1)

    def test_matching_ref_is_marked_matched_for_snv_indel_and_mnv(self):
        for pos, ref, alt in ((4, "T", "C"), (3, "GTA", "G"), (3, "GT", "AC")):
            with self.subTest(pos=pos, ref=ref):
                result = normalize_variant("1", pos, ref, alt, fetch_sequence=self._source())
                self.assertEqual(result.reference_check, "matched")

    def test_lowercase_input_is_accepted(self):
        result = normalize_variant("1", 4, "t", "c", fetch_sequence=self._source())
        self.assertEqual(result.reference_check, "matched")

    def test_mismatch_is_flagged_and_the_variant_is_returned_exactly_as_given(self):
        # Reference at 3..5 is GTA; the VCF claims GAA.
        result = normalize_variant("1", 3, "GAA", "G", fetch_sequence=self._source())
        self.assertEqual(result.reference_check, "mismatch")
        self.assertEqual((result.pos, result.ref, result.alt), (3, "GAA", "G"))
        self.assertFalse(result.changed)
        self.assertFalse(result.was_left_aligned)
        self.assertIn("1:4", result.reference_check_detail)
        self.assertIn("GAA", result.reference_check_detail)
        self.assertIn("mismatch", result.left_align_skipped_reason)

    def test_snv_mismatch_is_flagged_too(self):
        result = normalize_variant("1", 4, "A", "C", fetch_sequence=self._source())  # reference base at 4 is T
        self.assertEqual(result.reference_check, "mismatch")
        self.assertEqual((result.pos, result.ref, result.alt), (4, "A", "C"))

    def test_a_reference_N_is_not_a_mismatch(self):
        result = normalize_variant("1", 2, "C", "G", fetch_sequence=_window_source("CNGT", 1))
        self.assertEqual(result.reference_check, "matched")

    def test_wrong_build_style_mismatch_on_real_bases(self):
        # A real GRCh38 BRCA2 window with the VCF REF altered by one base.
        record = next(r for r in _RECORDS if r["accession"] == "VCV004919673")
        b = record["GRCh38"]
        wrong_ref = ("A" if b["ref"][0] != "A" else "C") + b["ref"][1:]
        result = normalize_variant(
            record["chrom"], b["pos"], wrong_ref, b["alt"], fetch_sequence=_record_source(record, "GRCh38")
        )
        self.assertEqual(result.reference_check, "mismatch")
        self.assertFalse(result.changed)

    def test_unreadable_reference_degrades_to_trimming_and_says_so(self):
        def broken(chrom, lo, hi):
            raise ExternalAPIError("Ensembl unavailable")

        result = normalize_variant("1", 100, "ATG", "ACG", fetch_sequence=broken)
        self.assertEqual((result.pos, result.ref, result.alt), (101, "T", "C"))  # reference-free trim still applied
        self.assertEqual(result.reference_check, "not_checked")
        self.assertIn("unavailable", result.reference_check_detail)

    def test_unreadable_reference_for_an_indel_keeps_the_input_equivalent(self):
        def broken(chrom, lo, hi):
            raise ExternalAPIError()

        result = normalize_variant("1", 103, "AA", "A", fetch_sequence=broken)
        self.assertEqual((result.pos, result.ref, result.alt), (103, "AA", "A"))
        self.assertFalse(result.was_left_aligned)
        self.assertEqual(result.reference_check, "not_checked")
        self.assertIn("could not be read", result.left_align_skipped_reason)

    def test_reference_that_fails_after_the_ref_check_keeps_the_equivalent_trimmed_form(self):
        reference = "GCACACAT"
        calls = []

        def flaky(chrom, lo, hi):
            calls.append((lo, hi))
            if len(calls) > 1:  # the REF check succeeds; the backward walk then fails
                raise ExternalAPIError("down")
            return reference[lo - 1 : hi]

        result = normalize_variant("1", 5, "ACA", "A", fetch_sequence=flaky)
        self.assertEqual((result.pos, result.ref, result.alt), (5, "ACA", "A"))
        self.assertFalse(result.was_left_aligned)
        self.assertEqual(result.reference_check, "matched")
        self.assertIn("unavailable", result.reference_check_detail)
        self.assertIn("could not be read", result.left_align_skipped_reason)

    def test_a_short_window_from_the_reference_source_is_treated_as_unreadable(self):
        result = normalize_variant("1", 103, "AA", "A", fetch_sequence=lambda c, lo, hi: "A")
        self.assertEqual(result.reference_check, "not_checked")

    def test_legacy_fetch_base_callers_are_validated_and_aligned_too(self):
        reference = "GCACACAT"
        result = normalize_variant("1", 5, "ACA", "A", fetch_base=lambda c, p: reference[p - 1])
        self.assertEqual((result.pos, result.ref, result.alt), (1, "GCA", "G"))
        self.assertEqual(result.reference_check, "matched")
        bad = normalize_variant("1", 5, "TCA", "T", fetch_base=lambda c, p: reference[p - 1])
        self.assertEqual(bad.reference_check, "mismatch")

    def test_no_reference_source_keeps_the_old_trim_only_behaviour(self):
        result = normalize_variant("1", 103, "AA", "A")
        self.assertEqual(result.reference_check, "not_checked")
        self.assertIsNone(result.reference_check_detail)
        self.assertIn("no reference sequence source", result.left_align_skipped_reason)

    def test_to_dict_is_additive(self):
        d = normalize_variant("1", 4, "T", "C", fetch_sequence=self._source()).to_dict()
        for key in ("chrom", "pos", "ref", "alt", "original_pos", "changed", "was_trimmed", "was_left_aligned"):
            self.assertIn(key, d)
        self.assertEqual(d["reference_check"], "matched")


# ---------------------------------------------------------------------------------------------------
# 6. Multiallelic records
# ---------------------------------------------------------------------------------------------------


class TestMultiallelicRecords(unittest.TestCase):
    def test_each_split_allele_is_normalised_independently_and_stays_equivalent(self):
        # Reference G C A C A C A T (1..8). One record, two ALTs at pos 5: a 2-base deletion and a 2-base insertion.
        reference = "GCACACAT"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "multi.vcf")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("##fileformat=VCFv4.2\n")
                fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
                fh.write("1\t5\t.\tACA\tA,ACACA\t50\tPASS\t.\n")
            variants = VCFParser(path).parse()
        self.assertEqual([(v.ref, v.alt) for v in variants], [("ACA", "A"), ("ACA", "ACACA")])
        source = _window_source(reference + "ACGT", 1)
        outputs = []
        for v in variants:
            r = normalize_variant("1", v.pos, v.ref, v.alt, fetch_sequence=source)
            self.assertEqual(r.reference_check, "matched")
            self.assertEqual(
                _apply(reference + "ACGT", 1, r.pos, r.ref, r.alt), _apply(reference + "ACGT", 1, v.pos, v.ref, v.alt)
            )
            outputs.append((r.pos, r.ref, r.alt))
        self.assertEqual(len(set(outputs)), 2)
        self.assertEqual(outputs[0], (1, "GCA", "G"))


# ---------------------------------------------------------------------------------------------------
# 7. Orchestrator wiring: the check is visible and uses the run's reference source
# ---------------------------------------------------------------------------------------------------


def _pipeline(fetch_sequence, assembly="GRCh38"):
    from pipeline.orchestrator import GeperPipeline

    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.sequence_context_gen = SimpleNamespace(assembly=assembly, fetch_reference_sequence=fetch_sequence)
    return p


def _variant(pos, ref, alt):
    return Variant(chrom="1", pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


class TestOrchestratorNormalizationStage(unittest.TestCase):
    REFERENCE = "GCACACAT"

    def test_indel_is_left_aligned_through_the_runs_own_reference_source(self):
        p = _pipeline(_window_source(self.REFERENCE, 1))
        errors = []
        result = p._run_normalization_stage(_variant(5, "ACA", "A"), errors)
        n = result["normalized"]
        self.assertEqual((n["pos"], n["ref"], n["alt"]), (1, "GCA", "G"))
        self.assertEqual(n["reference_check"], "matched")
        self.assertEqual(errors, [])

    def test_reference_mismatch_is_reported_in_the_run_warnings_and_the_variant_is_untouched(self):
        p = _pipeline(_window_source(self.REFERENCE, 1))
        errors = []
        result = p._run_normalization_stage(_variant(5, "TCA", "T"), errors)
        n = result["normalized"]
        self.assertEqual((n["pos"], n["ref"], n["alt"]), (5, "TCA", "T"))
        self.assertEqual(n["reference_check"], "mismatch")
        self.assertEqual(len(errors), 1)
        self.assertIn("Reference allele mismatch", errors[0])
        self.assertIn("NOT normalised", errors[0])
        self.assertIn("GRCh38", errors[0])

    def test_unreadable_reference_is_reported_not_swallowed(self):
        def broken(chrom, lo, hi):
            raise ExternalAPIError("down")

        p = _pipeline(broken, assembly="GRCh37")
        errors = []
        result = p._run_normalization_stage(_variant(5, "ACA", "A"), errors)
        self.assertEqual(result["normalized"]["reference_check"], "not_checked")
        self.assertEqual(len(errors), 1)
        self.assertIn("not checked", errors[0])

    def test_a_walk_that_fails_after_a_matching_ref_is_reported_as_incomplete_left_alignment(self):
        calls = []

        def flaky(chrom, lo, hi):
            calls.append((lo, hi))
            if len(calls) > 1:
                raise ExternalAPIError("down")
            return self.REFERENCE[lo - 1 : hi]

        p = _pipeline(flaky)
        errors = []
        result = p._run_normalization_stage(_variant(5, "ACA", "A"), errors)
        self.assertEqual(result["normalized"]["reference_check"], "matched")
        self.assertEqual(len(errors), 1)
        self.assertIn("left-alignment could not be completed", errors[0])

    def test_left_alignment_disabled_reads_no_reference(self):
        called = []
        p = _pipeline(lambda c, lo, hi: called.append((lo, hi)) or "A")
        with mock.patch("pipeline.orchestrator.CONFIG") as config:
            config.normalization.ENABLED = True
            config.normalization.LEFT_ALIGN_ENABLED = False
            config.normalization.LEFT_ALIGN_MAX_SHIFT_BP = 200
            errors = []
            result = p._run_normalization_stage(_variant(5, "ACA", "A"), errors)
        self.assertEqual(called, [])
        self.assertEqual(result["normalized"]["reference_check"], "not_checked")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
