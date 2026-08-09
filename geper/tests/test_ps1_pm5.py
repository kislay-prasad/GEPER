"""
Tests for the ACMG/AMP PS1 and PM5 rules (`pipeline/ps1_pm5/`, wired
into `pipeline/acmg_rules.py::ACMGRuleEngine._ps1` / `._pm5`).

Ground truth, not synthetic guesses. Every ClinVar record used here is
real, fetched live from NCBI E-utilities during development and frozen
in `tests/fixtures/ps1_pm5_clinvar_records.json` (verbatim uid,
accession, title, position, ref/alt, clinical significance, and review
status -- re-derivable from ClinVar directly, e.g.
https://www.ncbi.nlm.nih.gov/clinvar/variation/VCV000012374/). The
transcript structure (`tests/fixtures/ps1_pm5_transcript.json`) is the
real Ensembl MANE-equivalent TP53 transcript (ENST00000269305, matches
RefSeq NM_000546.6), including its real CDS sequence, so the codon
arithmetic these rules depend on is exercised end to end rather than
stubbed.

Three real TP53 codons anchor the ground truth:
  - Codon 175 (Arg, CGC): the classic DNE hotspot. Real anchor
    p.Arg175His (c.524G>A, Pathogenic, reviewed by expert panel).
  - Codon 237 (Met, ATG): three real ClinVar records at the SAME
    genomic position with three different ALT alleles, all producing
    p.Met237Ile -- the cleanest possible real demonstration of PS1's
    "regardless of nucleotide change" wording, and a genuine test of
    self-match exclusion at an identical position.
  - Codon 113 (Phe, TTC): every real record here is either
    single-submitter (1-star) or conflicting (0-star) for the
    Phe113Leu change specifically, while Phe113Ile/Phe113Val reach
    2-star -- real ground truth for both the confidence filter (PS1
    must not fire on 1-star/conflicting alone) and a genuine
    high-confidence PM5 positive at a codon other than 175.
"""

import json
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.ps1_pm5.decision import PS1PM5Evaluator
from pipeline.ps1_pm5.models import is_conflicting, star_rating
from pipeline.ps1_pm5.utils import (
    clinvar_codon_match_from_esummary,
    matches_from_clinvar_codon_result,
    parse_protein_change,
)
from pipeline.pvs1.utils import coding_consequence_detail, transcript_context_from_dict

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

with open(os.path.join(_FIXTURE_DIR, "ps1_pm5_transcript.json"), "r", encoding="utf-8") as _fh:
    _TRANSCRIPT_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "ps1_pm5_clinvar_records.json"), "r", encoding="utf-8") as _fh:
    _CLINVAR_RECORDS = json.load(_fh)


def transcript():
    return transcript_context_from_dict(_TRANSCRIPT_RECORD)


def _esummary_entry(record):
    """Reconstruct the esummary-shaped entry `clinvar_codon_match_from_esummary` actually parses, from the frozen fixture record."""
    return {
        "title": record["title"],
        "accession": record["accession"],
        "variation_set": [{"canonical_spdi": f"NC_000017.11:{record['pos'] - 1}:{record['ref']}:{record['alt']}"}],
        "germline_classification": {
            "description": record["clinical_significance"],
            "review_status": record["review_status"],
            "trait_set": [{"trait_name": c} for c in record["condition"]],
        },
    }


def matches_for(codon_label):
    """Every real ClinVar record at a codon, run through the real parser, as `pipeline/ps1_pm5/lookup.py` would return them."""
    out = []
    for record in _CLINVAR_RECORDS[codon_label]:
        match = clinvar_codon_match_from_esummary(record["uid"], _esummary_entry(record))
        if match is not None:
            out.append(match.to_dict())
    return out


def clinvar_codon_result(codon_label):
    return {"skipped": False, "found": True, "matches": matches_for(codon_label)}


def variant(pos, ref, alt):
    return {"chrom": "17", "pos": pos, "ref": ref, "alt": alt}


def transcript_result():
    return {"skipped": False, "found": True, "transcript": _TRANSCRIPT_RECORD}


# ---------------------------------------------------------------------------
# HGVS.p parsing -- the real ClinVar title strings, including the ones that
# must be REJECTED (frameshift/deletion), not just the ones that parse.
# ---------------------------------------------------------------------------


class TestProteinChangeParsing(unittest.TestCase):
    def test_parses_real_missense_titles(self):
        cases = [
            ("NM_000546.6(TP53):c.524G>A (p.Arg175His)", "R", 175, "H"),
            ("NM_000546.6(TP53):c.711G>A (p.Met237Ile)", "M", 237, "I"),
            ("NM_000546.6(TP53):c.337T>C (p.Phe113Leu)", "F", 113, "L"),
        ]
        for title, ref_aa, codon, alt_aa in cases:
            with self.subTest(title):
                parsed = parse_protein_change(title)
                self.assertEqual(parsed, {"ref_aa": ref_aa, "codon_number": codon, "alt_aa": alt_aa})

    def test_parses_synonymous_notation(self):
        self.assertEqual(
            parse_protein_change("NM_000546.6(TP53):c.525C>T (p.Arg175=)"),
            {"ref_aa": "R", "codon_number": 175, "alt_aa": "R"},
        )

    def test_rejects_frameshift_and_deletion_titles(self):
        for title in (
            "NM_000546.6(TP53):c.522dup (p.Arg175fs)",
            "NM_000546.6(TP53):c.711del (p.Met237fs)",
            "NM_000546.6(TP53):c.322_339del (p.Gly108_Phe113del)",
        ):
            with self.subTest(title):
                self.assertIsNone(parse_protein_change(title))

    def test_rejects_title_with_no_protein_change_at_all(self):
        self.assertIsNone(parse_protein_change("NM_000546.6(TP53):c.560-1G>A"))
        self.assertIsNone(parse_protein_change(None))


class TestClinVarCodonMatchParsing(unittest.TestCase):
    """`clinvar_codon_match_from_esummary` against the real fixture records end to end."""

    def test_real_missense_records_parse_with_correct_fields(self):
        matches = matches_for("codon175")
        by_uid = {m["uid"]: m for m in matches}
        self.assertEqual(by_uid["12374"]["protein_change"], "R175H")
        self.assertEqual(by_uid["12374"]["star_rating"], star_rating("reviewed by expert panel"))
        self.assertEqual(by_uid["12374"]["star_rating"], 3)
        self.assertFalse(by_uid["12374"]["is_conflicting"])
        self.assertEqual(by_uid["4189264"]["star_rating"], 0)
        self.assertTrue(by_uid["4189264"]["is_conflicting"])

    def test_frameshift_record_is_dropped_not_misparsed(self):
        matches = matches_for("codon175")
        uids = {m["uid"] for m in matches}
        self.assertNotIn("2687679", uids)  # c.522dup (p.Arg175fs)

    def test_star_rating_and_conflicting_helpers_match_real_review_strings(self):
        self.assertEqual(star_rating("reviewed by expert panel"), 3)
        self.assertEqual(star_rating("criteria provided, multiple submitters, no conflicts"), 2)
        self.assertEqual(star_rating("criteria provided, single submitter"), 1)
        self.assertEqual(star_rating("criteria provided, conflicting classifications"), 0)
        self.assertTrue(is_conflicting("criteria provided, conflicting classifications"))
        self.assertFalse(is_conflicting("reviewed by expert panel"))


# ---------------------------------------------------------------------------
# Codon coordinate math (shared with PVS1, re-verified here for TP53 specifically)
# ---------------------------------------------------------------------------


class TestTP53CodonMath(unittest.TestCase):
    def test_maps_real_clinvar_positions_to_expected_amino_acid_changes(self):
        t = transcript()
        cases = [
            (7675088, "C", "T", "R", 175, "H"),  # c.524G>A
            (7675089, "G", "A", "R", 175, "C"),  # c.523C>T
            (7674252, "C", "T", "M", 237, "I"),  # c.711G>A
            (7674252, "C", "A", "M", 237, "I"),  # c.711G>T
            (7676032, "A", "G", "F", 113, "L"),  # c.337T>C
        ]
        for pos, ref, alt, ref_aa, codon, alt_aa in cases:
            with self.subTest((pos, ref, alt)):
                detail = coding_consequence_detail(t, pos, ref, alt)
                self.assertEqual((detail.ref_aa, detail.codon_number, detail.alt_aa), (ref_aa, codon, alt_aa))


# ---------------------------------------------------------------------------
# PS1 -- real ground truth
# ---------------------------------------------------------------------------


class TestPS1KnownVariants(unittest.TestCase):
    def test_met237ile_via_a_different_allele_triggers_ps1(self):
        """
        Real ClinVar: codon 237 has THREE records at the identical
        genomic position (chr17:7674252), one ALT allele each, all
        producing p.Met237Ile:
          c.711G>A -- Pathogenic, reviewed by expert panel (VCV000142714)
          c.711G>T -- Pathogenic/Likely pathogenic, multi-submitter (VCV000634770)
          c.711G>C -- Pathogenic/Likely pathogenic, multi-submitter (VCV001757269)
        Querying with c.711G>T (itself one of the three real records)
        must exclude itself and still find the other two as anchors --
        the cleanest real test of "same amino acid change... regardless
        of nucleotide change" at the DNA level.
        """
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7674252, "C", "A")
        result = evaluator.evaluate_ps1(detail, matches_for("codon237"), transcript(), 7674252, "C", "A")
        self.assertTrue(result.applies)
        anchor_uids = {m["uid"] for m in result.matched_anchors}
        self.assertEqual(anchor_uids, {"142714", "1757269"})  # excludes 634770, its own record
        self.assertTrue(any("VCV000142714" in line for line in result.supporting_evidence))
        self.assertIn("M237I", result.rationale)

    def test_engine_reports_ps1_strong_for_met237ile(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant(7674252, "C", "A"),
            transcript_result=transcript_result(),
            clinvar_codon_result=clinvar_codon_result("codon237"),
        )
        ps1 = result["all_criteria"]["PS1"]
        self.assertEqual(ps1["status"], "triggered")
        self.assertEqual(ps1["strength"], "strong")
        self.assertIn("142714", [m["uid"] for m in ps1["details"]["matched_anchors"]])

    def test_arg175cys_does_not_trigger_ps1_no_matching_amino_acid(self):
        """Arg175Cys has no OTHER ClinVar record at codon 175 also producing Cys -- PS1 must not fire (that's PM5's job)."""
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7675089, "G", "A")
        result = evaluator.evaluate_ps1(detail, matches_for("codon175"), transcript(), 7675089, "G", "A")
        self.assertFalse(result.applies)

    def test_confidence_filter_blocks_ps1_on_single_submitter_and_conflicting_only(self):
        """
        Real ClinVar: both Phe113Leu records (c.337T>C, c.339C>G) are
        single-submitter (1-star); the only OTHER Phe113Leu record
        (c.339C>A) is Conflicting (0-star). None meet the default
        2-star bar, so PS1 must not fire even though a real
        amino-acid match exists -- this is the confidence-filter
        requirement the task calls out explicitly.
        """
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7676032, "A", "G")  # c.337T>C, Phe113Leu
        result = evaluator.evaluate_ps1(detail, matches_for("codon113"), transcript(), 7676032, "A", "G")
        self.assertFalse(result.applies)
        self.assertTrue(result.rejected_anchors)  # seen, not silently absent
        self.assertIn("confidence bar", result.rationale)

    def test_ps1_requires_a_missense_query_not_synonymous(self):
        # Real ClinVar: c.525C>T (p.Arg175=) is at 1-based genomic
        # 7675087 (SPDI 7675086 is 0-based; 1-based = SPDI + 1).
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7675087, "G", "A")
        self.assertEqual(detail.category, "synonymous")
        result = evaluator.evaluate_ps1(detail, matches_for("codon175"), transcript(), 7675087, "G", "A")
        self.assertFalse(result.applies)
        self.assertIn("synonymous", result.rationale)


# ---------------------------------------------------------------------------
# PM5 -- real ground truth
# ---------------------------------------------------------------------------


class TestPM5KnownVariants(unittest.TestCase):
    def test_arg175cys_triggers_pm5_citing_arg175his(self):
        """
        Real ClinVar: c.523C>T (p.Arg175Cys) is itself classified
        Uncertain significance despite expert-panel review -- and that
        is irrelevant to PM5, which is evidence drawn from OTHER
        variants at the codon, not a restatement of ClinVar's own call
        on this exact variant. The real anchor is p.Arg175His
        (c.524G>A, Pathogenic, expert panel).
        """
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7675089, "G", "A")
        result = evaluator.evaluate_pm5(detail, matches_for("codon175"), transcript(), 7675089, "G", "A")
        self.assertTrue(result.applies)
        anchor_uids = {m["uid"] for m in result.matched_anchors}
        self.assertIn("12374", anchor_uids)  # Arg175His
        self.assertNotIn("245851", anchor_uids)  # never its own record
        self.assertTrue(
            any("not fully modeled" in c or "Substitution-similarity" in c for c in result.unchecked_caveats)
        )

    def test_met237ile_does_not_trigger_pm5_against_itself(self):
        """The same amino acid as every anchor at this codon is PS1's evidence, not PM5's."""
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7674252, "C", "A")
        result = evaluator.evaluate_pm5(detail, matches_for("codon237"), transcript(), 7674252, "C", "A")
        self.assertFalse(result.applies)

    def test_phe113leu_triggers_pm5_via_high_confidence_ile_and_val_anchors(self):
        """
        Real ClinVar: codon 113's Phe113Leu records are all 1-star or
        conflicting (see the PS1 confidence-filter test above), but
        Phe113Ile (c.337T>A) and Phe113Val (c.337T>G) both reach 2-star
        (multiple submitters, no conflicts) at the SAME codon -- a
        genuine, independent high-confidence PM5 positive at a codon
        other than 175.
        """
        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7676030, "G", "C")  # c.339C>G, Phe113Leu
        result = evaluator.evaluate_pm5(detail, matches_for("codon113"), transcript(), 7676030, "G", "C")
        self.assertTrue(result.applies)
        anchor_uids = {m["uid"] for m in result.matched_anchors}
        self.assertEqual(anchor_uids, {"1469551", "141302"})  # Phe113Ile, Phe113Val

    def test_engine_reports_pm5_moderate_and_flags_substitution_caveat(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant(7675089, "G", "A"),
            transcript_result=transcript_result(),
            clinvar_codon_result=clinvar_codon_result("codon175"),
        )
        pm5 = result["all_criteria"]["PM5"]
        self.assertEqual(pm5["status"], "triggered")
        self.assertEqual(pm5["strength"], "moderate")
        self.assertTrue(pm5["details"]["unchecked_caveats"])

    def test_frameshift_record_is_dropped_before_it_ever_reaches_pm5(self):
        """
        A frameshift/indel record at the same codon (real: c.711del,
        p.Met237fs, VCV000663828, itself classified Pathogenic) is
        neither the same nor a different MISSENSE change and must
        never count as evidence for either rule. It is filtered even
        earlier than the PM5 comparison itself: `parse_protein_change`
        has no pattern for "fs" notation, so
        `clinvar_codon_match_from_esummary` already drops it -- the
        raw match list PM5 receives never contains it at all, which is
        the stronger and more correct guarantee (see
        `TestProteinChangeParsing.test_rejects_frameshift_and_deletion_titles`
        for that in isolation).
        """
        matches = matches_for("codon237")
        self.assertNotIn("663828", {m["uid"] for m in matches})

        evaluator = PS1PM5Evaluator()
        detail = coding_consequence_detail(transcript(), 7674252, "C", "A")  # Met237Ile
        result = evaluator.evaluate_pm5(detail, matches, transcript(), 7674252, "C", "A")
        self.assertNotIn("663828", {m["uid"] for m in result.matched_anchors})


# ---------------------------------------------------------------------------
# Splice-proximity caveat (structurally real: TP53's split codon 187)
# ---------------------------------------------------------------------------


class TestSpliceProximityCaveat(unittest.TestCase):
    """
    TP53 codon 187 (GGT=Gly) genuinely straddles intron 5: its first
    base is the last base of exon 5, its other two are the first two
    bases of exon 6 (confirmed via the real transcript structure --
    `TranscriptContext.genomic_positions_for_codon(187)` returns three
    non-contiguous genomic positions with a whole intron between them).
    Any missense substitution there sits at distance 0 from a splice
    junction. The anchor used here is constructed (clearly labeled) to
    isolate this one caveat; the codon's splice adjacency itself is
    real transcript structure, not an assumption.
    """

    def test_ps1_pm5_withheld_for_a_variant_at_a_real_split_codon(self):
        t = transcript()
        positions = t.genomic_positions_for_codon(187)
        self.assertEqual(positions, [7675053, 7674971, 7674970])
        self.assertEqual(t.distance_to_nearest_exon_boundary(7675053), 0)

        constructed_anchor = [
            {
                "uid": "constructed",
                "accession": None,
                "title": "NM_000546.6(TP53):c.560G>A (p.Gly187Asp) [constructed for this test]",
                "pos": 7674971,
                "ref": "G",
                "alt": "A",
                "protein_change": "G187D",
                "codon_number": 187,
                "clinical_significance": "Pathogenic",
                "review_status": "reviewed by expert panel",
                "star_rating": 3,
                "is_conflicting": False,
                "condition": [],
            }
        ]
        detail = coding_consequence_detail(t, 7675053, "C", "G")  # genomic C>G at exon5's last base
        result = PS1PM5Evaluator().evaluate_pm5(detail, constructed_anchor, t, 7675053, "C", "G")
        self.assertFalse(result.applies)
        self.assertIn("exon-intron junction", result.rationale)
        self.assertTrue(any("0 bp" in c for c in result.caveats_checked))

    def test_same_anchor_far_from_any_boundary_is_not_blocked(self):
        """Control: codon 175 is safely interior (exon 5, codons 126-187) -- the caveat must not fire there."""
        t = transcript()
        self.assertGreater(t.distance_to_nearest_exon_boundary(7675088), 3)
        constructed_anchor = [
            {
                "uid": "constructed2",
                "accession": None,
                "title": "constructed control anchor",
                "pos": 7675088,
                "ref": "C",
                "alt": "T",
                "protein_change": "R175D",
                "codon_number": 175,
                "clinical_significance": "Pathogenic",
                "review_status": "reviewed by expert panel",
                "star_rating": 3,
                "is_conflicting": False,
                "condition": [],
            }
        ]
        detail = coding_consequence_detail(t, 7675089, "G", "C")  # Arg175Gly
        result = PS1PM5Evaluator().evaluate_pm5(detail, constructed_anchor, t, 7675089, "G", "C")
        self.assertTrue(result.applies)


# ---------------------------------------------------------------------------
# Rule-engine wiring / backward compatibility
# ---------------------------------------------------------------------------


class TestEngineWiring(unittest.TestCase):
    def test_engine_still_works_for_callers_that_pass_no_ps1_pm5_inputs(self):
        result = ACMGRuleEngine().evaluate()
        self.assertEqual(result["all_criteria"]["PS1"]["status"], "not_evaluated")
        self.assertEqual(result["all_criteria"]["PM5"]["status"], "not_evaluated")

    def test_no_clinvar_records_at_codon_is_not_evaluated_not_not_triggered(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant(7674252, "C", "A"),
            transcript_result=transcript_result(),
            clinvar_codon_result={"skipped": False, "found": False, "matches": []},
        )
        self.assertEqual(result["all_criteria"]["PS1"]["status"], "not_evaluated")

    def test_indel_is_not_triggered_not_not_evaluated(self):
        """
        Regression test for I8 (report review round 4): an indel isn't
        a substitution at all, so PS1/PM5 don't apply to it -- that's a
        class-inapplicability, the same "checked, and the answer is no"
        bucket a nonsense/synonymous SNV already gets, not a genuine
        gap. Real ground truth: `test_data/conflict_tiers.vcf` Finding
        5 is VHL c.422dup, an insertion at 3:10146594 -- this uses the
        same TP53 fixture transcript with a synthetic 2-base insertion
        at codon 175's position to isolate the "is this a SNV at all"
        gate from any VHL-specific transcript-fetch questions.
        """
        result = ACMGRuleEngine().evaluate(
            variant_dict=variant(7674221, "C", "CAA"),  # insertion, not a SNV
            transcript_result=transcript_result(),
            clinvar_codon_result={"skipped": False, "found": False, "matches": []},
        )
        for code in ("PS1", "PM5"):
            self.assertEqual(result["all_criteria"][code]["status"], "not_triggered")
            self.assertIn("not a single-nucleotide substitution", result["all_criteria"][code]["rationale"])

    def test_matches_from_clinvar_codon_result_handles_skipped_and_missing(self):
        self.assertEqual(matches_from_clinvar_codon_result(None), [])
        self.assertEqual(matches_from_clinvar_codon_result({"skipped": True}), [])
        self.assertEqual(matches_from_clinvar_codon_result({"error": "boom"}), [])


if __name__ == "__main__":
    unittest.main()
