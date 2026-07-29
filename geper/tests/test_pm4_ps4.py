"""
Tests for the ACMG/AMP PM4 and PS4 rules (`pipeline/acmg_rules.py::
ACMGRuleEngine._pm4` / `._ps4`, `pipeline/pvs1/utils.py::
classify_pm4_variant`).

Ground truth, not synthetic guesses, for PM4's core variant-type
detection:
  - HBA2 c.427T>C (p.Ter143Gln) -- Hemoglobin Constant Spring, the
    textbook real pathogenic stop-loss variant (ClinVar VCV000012822-
    equivalent record, "Pathogenic", reviewed by expert panel). A
    single-nucleotide substitution converting HBA2's natural stop
    codon to glutamine, causing translational read-through and a
    31-residue C-terminal extension -- alpha-thalassemia.
  - TP53 c.792_794del (p.Leu265del) -- real ClinVar record, "Likely
    pathogenic", criteria provided/multiple submitters/no conflicts. A
    clean single-residue (3 bp) in-frame deletion.

The repeat-region caveat is grounded in real UniProt data (Titin/
Q8WZ42's PEVK repeat, the field-standard example of a large,
well-documented repeat region tolerating in-frame length variation --
see `tests/fixtures/pm4_uniprot_ttn_repeat_features.json`'s own
provenance note for why the specific *test variant* paired with it is
constructed rather than a cross-referenced real ClinVar record: TTN's
clinically-used transcripts (e.g. the cardiac N2AB isoform) use
different residue numbering than the canonical full-length isoform
UniProt annotates, and no genomic-coordinate cross-check was performed
to confidently bridge the two for a specific real variant. Stated
honestly rather than forcing a shaky match.

PS4 has no ground truth to test against a "triggered" outcome at all,
by design: GEPER integrates no case-frequency source, so PS4 can never
trigger. The tests here instead verify that invariant holds regardless
of input, and that the real gnomAD context PS4 does surface is
accurate and clearly labeled as informational only.
"""

import json
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.pvs1.utils import (
    PM4_IN_FRAME_INDEL,
    PM4_STOP_LOSS,
    classify_pm4_variant,
    transcript_context_from_dict,
)

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

with open(os.path.join(_FIXTURE_DIR, "pm4_hba2_transcript.json"), "r", encoding="utf-8") as _fh:
    _HBA2_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "ps1_pm5_transcript.json"), "r", encoding="utf-8") as _fh:
    _TP53_RECORD = json.load(_fh)["transcript"]

with open(os.path.join(_FIXTURE_DIR, "pm4_uniprot_ttn_repeat_features.json"), "r", encoding="utf-8") as _fh:
    _TTN_UNIPROT = json.load(_fh)


def hba2():
    return transcript_context_from_dict(_HBA2_RECORD)


def tp53():
    return transcript_context_from_dict(_TP53_RECORD)


def hba2_transcript_result():
    return {"skipped": False, "found": True, "transcript": _HBA2_RECORD}


def tp53_transcript_result():
    return {"skipped": False, "found": True, "transcript": _TP53_RECORD}


def uniprot_result(features=None, found=True):
    return {"skipped": False, "found": found, "gene_symbol": "TEST", "features": features or []}


# ---------------------------------------------------------------------------
# classify_pm4_variant -- pure classification, real coordinates
# ---------------------------------------------------------------------------

class TestClassifyPM4Variant(unittest.TestCase):
    def test_hemoglobin_constant_spring_is_classified_stop_loss(self):
        """Real ClinVar: HBA2 c.427T>C (p.Ter143Gln), Pathogenic, reviewed by expert panel."""
        detail, notes = classify_pm4_variant({"pos": 173598, "ref": "T", "alt": "C"}, hba2())
        self.assertIsNotNone(detail)
        self.assertEqual(detail.category, PM4_STOP_LOSS)
        self.assertEqual(detail.codon_number, 143)
        self.assertTrue(any("stop-loss" in n for n in notes))

    def test_tp53_leu265del_is_classified_in_frame_indel(self):
        """Real ClinVar: TP53 c.792_794del (p.Leu265del), Likely pathogenic, multiple submitters no conflicts."""
        detail, notes = classify_pm4_variant({"pos": 7673826, "ref": "AGTAG", "alt": "AG"}, tp53())
        self.assertIsNotNone(detail)
        self.assertEqual(detail.category, PM4_IN_FRAME_INDEL)
        self.assertEqual(detail.codon_number, 265)
        self.assertEqual(detail.residues_changed, 1)

    def test_frameshift_indel_is_not_classified_as_pm4(self):
        """A 1 bp deletion (not a multiple of 3) is PVS1/frameshift territory, not PM4's."""
        detail, notes = classify_pm4_variant({"pos": 7673826, "ref": "AG", "alt": "A"}, tp53())
        self.assertIsNone(detail)
        self.assertTrue(any("frameshift" in n for n in notes))

    def test_ordinary_missense_is_not_classified_as_pm4(self):
        """TP53 c.524G>A (p.Arg175His) -- a plain substitution, not at the stop codon."""
        detail, notes = classify_pm4_variant({"pos": 7675088, "ref": "C", "alt": "T"}, tp53())
        self.assertIsNone(detail)

    def test_no_transcript_yields_no_classification(self):
        detail, notes = classify_pm4_variant({"pos": 173598, "ref": "T", "alt": "C"}, None)
        self.assertIsNone(detail)
        self.assertEqual(notes, [])


# ---------------------------------------------------------------------------
# PM4 -- real ClinVar ground truth, through the full rule engine
# ---------------------------------------------------------------------------

class TestPM4KnownVariants(unittest.TestCase):
    def test_hemoglobin_constant_spring_triggers_pm4(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "16", "pos": 173598, "ref": "T", "alt": "C"},
            transcript_result=hba2_transcript_result(),
        )
        pm4 = result["all_criteria"]["PM4"]
        self.assertEqual(pm4["status"], "triggered")
        self.assertEqual(pm4["strength"], "moderate")
        self.assertEqual(pm4["details"]["variant_detail"]["category"], PM4_STOP_LOSS)
        self.assertIn("stop loss", pm4["rationale"])

    def test_tp53_leu265del_triggers_pm4(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
            transcript_result=tp53_transcript_result(),
        )
        pm4 = result["all_criteria"]["PM4"]
        self.assertEqual(pm4["status"], "triggered")
        self.assertEqual(pm4["details"]["variant_detail"]["codon_number"], 265)
        self.assertEqual(pm4["details"]["variant_detail"]["residues_changed"], 1)

    def test_frameshift_does_not_trigger_pm4(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AG", "alt": "A"},
            transcript_result=tp53_transcript_result(),
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "not_triggered")

    def test_missense_does_not_trigger_pm4(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7675088, "ref": "C", "alt": "T"},
            transcript_result=tp53_transcript_result(),
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "not_triggered")

    def test_missing_transcript_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "not_evaluated")

    def test_no_uniprot_data_still_triggers_but_flags_the_gap(self):
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "16", "pos": 173598, "ref": "T", "alt": "C"},
            transcript_result=hba2_transcript_result(),
        )
        pm4 = result["all_criteria"]["PM4"]
        self.assertEqual(pm4["status"], "triggered")
        self.assertEqual(pm4["confidence"], "Low")
        self.assertTrue(any("no uniprot annotation" in c.lower() for c in pm4["details"]["unchecked_caveats"]))


class TestPM4RepeatRegionCaveat(unittest.TestCase):
    """
    Grounded in real UniProt data (Titin/Q8WZ42's PEVK repeat -- see
    this module's own docstring for why the paired test variant is
    constructed, not a cross-referenced real ClinVar record).
    """

    def test_in_frame_deletion_inside_a_real_uniprot_repeat_span_is_blocked(self):
        pevk_1 = _TTN_UNIPROT["features"][2]  # "PEVK 1", residues 10216-10242 (real UniProt annotation)
        self.assertEqual(pevk_1["description"], "PEVK 1")
        # A constructed TP53 in-frame deletion whose codon is set to
        # fall inside that real repeat span, isolating the caveat.
        features = [dict(pevk_1, begin=260, end=270)]  # same real feature, remapped onto TP53's own codon range for this isolated test
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},  # codon 265
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        pm4 = result["all_criteria"]["PM4"]
        self.assertEqual(pm4["status"], "not_triggered")
        self.assertIn("repeat", pm4["rationale"].lower())
        self.assertIn("PEVK 1", pm4["rationale"])

    def test_repeat_elsewhere_in_the_gene_does_not_block_pm4(self):
        features = [{"feature_type": "Repeat", "description": "unrelated repeat", "begin": 1, "end": 50}]
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},  # codon 265, outside 1-50
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "triggered")

    def test_compositional_bias_feature_also_blocks_pm4(self):
        """UniProt's low-complexity 'Compositional bias' type is treated the same as 'Repeat' -- both are population-tolerant, non-functional-domain regions."""
        features = [{"feature_type": "Compositional bias", "description": "Pro residues", "begin": 260, "end": 270}]
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "not_triggered")

    def test_a_structural_domain_feature_does_not_block_pm4(self):
        """A UniProt 'Domain' annotation (structurally significant, the opposite of a repeat) must not trigger the caveat."""
        features = [{"feature_type": "Domain", "description": "DNA-binding domain", "begin": 260, "end": 270}]
        result = ACMGRuleEngine().evaluate(
            variant_dict={"chrom": "17", "pos": 7673826, "ref": "AGTAG", "alt": "AG"},
            transcript_result=tp53_transcript_result(),
            uniprot_result=uniprot_result(features),
        )
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "triggered")


# ---------------------------------------------------------------------------
# PS4 -- honest, always-not_evaluated behavior
# ---------------------------------------------------------------------------

class TestPS4NeverTriggers(unittest.TestCase):
    """PS4 requires case-frequency data GEPER does not integrate; it must never report 'triggered', under any input."""

    def test_no_gnomad_data_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(gnomad_result=None)
        ps4 = result["all_criteria"]["PS4"]
        self.assertEqual(ps4["status"], "not_evaluated")
        self.assertIn("case-frequency source", ps4["rationale"])

    def test_common_variant_is_not_evaluated_with_context(self):
        result = ACMGRuleEngine().evaluate(
            gnomad_result={"skipped": False, "found": True, "global_af": 0.05, "population_breakdown": {}},
        )
        ps4 = result["all_criteria"]["PS4"]
        self.assertEqual(ps4["status"], "not_evaluated")
        self.assertIn("very unlikely to hold", ps4["rationale"])
        self.assertAlmostEqual(ps4["details"]["gnomad_allele_frequency"], 0.05)

    def test_rare_variant_is_not_evaluated_with_context(self):
        result = ACMGRuleEngine().evaluate(
            gnomad_result={"skipped": False, "found": True, "global_af": 1e-6, "population_breakdown": {}},
        )
        ps4 = result["all_criteria"]["PS4"]
        self.assertEqual(ps4["status"], "not_evaluated")
        self.assertIn("not itself evidence of case enrichment", ps4["rationale"])

    def test_absent_from_gnomad_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(gnomad_result={"skipped": False, "found": False})
        self.assertEqual(result["all_criteria"]["PS4"]["status"], "not_evaluated")

    def test_never_triggers_across_a_sweep_of_allele_frequencies(self):
        for af in (0.0, 1e-8, 1e-6, 1e-5, 1e-4, 0.001, 0.01, 0.1, 0.5):
            with self.subTest(af=af):
                result = ACMGRuleEngine().evaluate(
                    gnomad_result={"skipped": False, "found": True, "global_af": af, "population_breakdown": {}},
                )
                self.assertNotEqual(result["all_criteria"]["PS4"]["status"], "triggered")

    def test_case_frequency_source_is_explicitly_none_in_details(self):
        result = ACMGRuleEngine().evaluate(
            gnomad_result={"skipped": False, "found": True, "global_af": 1e-6, "population_breakdown": {}},
        )
        self.assertIsNone(result["all_criteria"]["PS4"]["details"]["case_frequency_source"])


# ---------------------------------------------------------------------------
# Engine wiring / backward compatibility
# ---------------------------------------------------------------------------

class TestEngineWiring(unittest.TestCase):
    def test_engine_still_works_for_callers_that_pass_no_pm4_ps4_inputs(self):
        result = ACMGRuleEngine().evaluate()
        self.assertEqual(result["all_criteria"]["PM4"]["status"], "not_evaluated")
        self.assertEqual(result["all_criteria"]["PS4"]["status"], "not_evaluated")

    def test_pm4_and_ps4_appear_in_all_criteria_with_correct_strengths(self):
        result = ACMGRuleEngine().evaluate()
        self.assertEqual(result["all_criteria"]["PM4"]["strength"], "moderate")
        self.assertEqual(result["all_criteria"]["PS4"]["strength"], "strong")


if __name__ == "__main__":
    unittest.main()
