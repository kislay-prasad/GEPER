"""
HIGH 3 / T3-F3 (2026-08-28, ruled): before this fix, three call sites
independently reworded the identical frameshift-vs-nonsense fact --
`InterpretationEngine.interpret()`'s legacy evidence line said
"premature stop codon (nonsense)"; `pipeline/pvs1/utils.py::
classify_null_variant`'s local-translation fallback said the same
thing with a different lead-in verb; `ACMGRuleEngine.
_pp3_bp4_inapplicability_reason` alone said "nonsense (stop-gained)".
All three now source the term from `pipeline/pvs1/utils.py::
null_variant_term` -- this file pins that they agree, and that the
canonical term is literally "nonsense (stop-gained)" (Q4-E: keeping
both halves of the gloss deliberately, not the older "premature stop
codon" phrasing).

Before this fix, `test_all_three_sites_agree_on_the_nonsense_term`
below would have failed: legacy's own sentence never contained
"nonsense (stop-gained)" at all (it said "premature stop codon
(nonsense)"), so the shared substring could not have been found across
all three texts.
"""

import json
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.pvs1.utils import classify_null_variant, null_variant_term

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pvs1_transcripts.json")
with open(_FIXTURE, "r", encoding="utf-8") as _fh:
    _TRANSCRIPTS = json.load(_fh)["transcripts"]


def _brca1_transcript_result():
    """Same real, frozen MANE Select BRCA1 structure `tests/test_pvs1.py`
    uses -- needed so `protein_effect_flags`'s CDS-frame call (the
    determination legacy's own evidence line uses) can actually resolve
    a nonsense/missense/synonymous category, unlike the indel-length
    parity path (frameshift), which needs no transcript at all."""
    record = dict(_TRANSCRIPTS["BRCA1"])
    return {"skipped": False, "found": True, "gene_symbol": "BRCA1", "source": "fixture", "transcript": record}


def _nonsense_protein_result():
    return {"skipped": False, "translation": {"ref_protein": "MAAAQ", "alt_protein": "MAA*"}}


def _frameshift_protein_result():
    return {"skipped": False, "translation": {"ref_protein": "MAAAQ", "alt_protein": "MAAH"}}


class TestNullVariantTermUnit(unittest.TestCase):
    def test_frameshift_term(self):
        self.assertEqual(null_variant_term(True), "frameshift")

    def test_nonsense_term(self):
        self.assertEqual(null_variant_term(False), "nonsense (stop-gained)")


class TestAllThreeSitesAgree(unittest.TestCase):
    """The three original T3-F3 sites, exercised through their real
    production code paths (not by calling `null_variant_term` a fourth
    time and comparing it to itself)."""

    _CANONICAL_NONSENSE = "nonsense (stop-gained)"
    _CANONICAL_FRAMESHIFT = "frameshift"

    def test_legacy_interpretation_sentence_uses_canonical_nonsense_term(self):
        # Real BRCA1 nonsense variant + real MANE Select transcript
        # structure (same fixture `tests/test_pvs1.py` uses for this
        # exact coordinate) -- `protein_effect_flags`'s CDS-frame call
        # needs a real transcript to resolve nonsense, unlike the
        # frameshift case below (indel length parity alone).
        engine = InterpretationEngine()
        result = engine.interpret(
            variant_dict={"chrom": "17", "pos": 43093844, "ref": "G", "alt": "A"},
            dna_models_used=[],
            clinvar_result={},
            dbsnp_result={},
            protein_result=_nonsense_protein_result(),
            blast_result={},
            transcript_result=_brca1_transcript_result(),
        )
        lof_lines = [e for e in result["supporting_evidence"] if "Transcript-verified protein consequence" in e]
        self.assertTrue(lof_lines, "fixture did not produce a protein-consequence evidence line")
        self.assertIn(self._CANONICAL_NONSENSE, lof_lines[0])
        self.assertNotIn("premature stop codon", lof_lines[0])

    def test_legacy_interpretation_sentence_uses_canonical_frameshift_term(self):
        engine = InterpretationEngine()
        result = engine.interpret(
            variant_dict={"chrom": "17", "pos": 43093844, "ref": "GA", "alt": "G"},
            dna_models_used=[],
            clinvar_result={},
            dbsnp_result={},
            protein_result=_frameshift_protein_result(),
            blast_result={},
            transcript_result={"skipped": True, "found": False},
        )
        lof_lines = [e for e in result["supporting_evidence"] if "Transcript-verified protein consequence" in e]
        self.assertTrue(lof_lines)
        self.assertIn(self._CANONICAL_FRAMESHIFT, lof_lines[0])

    def test_pvs1_local_translation_fallback_uses_canonical_nonsense_term(self):
        null_type, notes = classify_null_variant(
            {"pos": 100, "ref": "G", "alt": "T"}, protein_result=_nonsense_protein_result(), transcript=None
        )
        self.assertTrue(any(self._CANONICAL_NONSENSE in n for n in notes))
        self.assertFalse(any("introduces a premature stop codon" in n for n in notes))

    def test_pvs1_local_translation_fallback_uses_canonical_frameshift_term(self):
        null_type, notes = classify_null_variant(
            {"pos": 100, "ref": "GA", "alt": "G"}, protein_result=_frameshift_protein_result(), transcript=None
        )
        self.assertTrue(any(self._CANONICAL_FRAMESHIFT in n for n in notes))

    def test_real_acmg_inapplicability_reason_uses_canonical_nonsense_term(self):
        class _Flags:
            determined = True
            is_lof = True

        reason = ACMGRuleEngine._pp3_bp4_inapplicability_reason(
            protein_flags=_Flags(),
            null_variant_type=None,
            variant_dict={"ref": "G", "alt": "T"},
        )
        self.assertIsNotNone(reason)
        self.assertIn(self._CANONICAL_NONSENSE, reason)


if __name__ == "__main__":
    unittest.main()
