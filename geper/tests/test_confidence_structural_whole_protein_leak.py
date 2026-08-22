"""
PRE-FIX regression test, written before the fix exists (dispatch: "capture the
dangerous case RED before Andy strips the fallback").

The defect: `pipeline/confidence_engine.py::ConfidenceEngine._structural_quality`
(line 466) reads

    alphafold_result.get("affected_residue_band") or alphafold_result.get("mean_plddt_band") or ""

so when a variant's own residue-specific AlphaFold confidence band is absent,
the WHOLE-PROTEIN AVERAGE band silently takes its place and feeds the rendered
Confidence table's Structural Biology figures. Per AlphaFold lookup's own
fallback logic (`pipeline/alphafold/lookup.py::AlphaFoldLookup.query_variant`,
which falls back to the accession-level, position-independent lookup whenever
`canonical_protein_position` is None), this is essentially every splice-site
and intronic variant with an AlphaFold hit.

The project has already named this exact defect a FABRICATED CLAIM at a
sibling site that got the fix -- see
`pipeline/conflict_resolution_engine.py::_structural_conflict`'s own docstring
(lines 619-638): treating a large, mostly-disordered protein's whole-protein
average confidence as "confidence at the affected residue" fabricates a claim
about a position AlphaFold was never actually asked about. That sibling site
reads ONLY `affected_residue_band`, with no `mean_plddt_band` fallback at all.
`confidence_engine.py:466` never got the same fix.

This test does not assert on the fix's internal shape (the human specifically
ruled against pinning `band == ""` as the absent-case representation, since
`or ""` is the same truthiness-for-presence defect one layer along -- pinning
it would reproduce present-but-empty inside the fix meant to remove it).
It asserts on what a reader of the rendered Confidence table can actually
observe: the whole-protein band's own value must not appear in the Structural
Biology category's rationale text, and the quality score must not be the one
that value alone would justify.

Pre-fix: RED. Andy strips the fallback at confidence_engine.py:466; this test
then goes green, and that is the proof the fix landed. Do not fix this here.
"""

import unittest

from pipeline.confidence_engine import ConfidenceEngine


class TestStructuralQualityDoesNotFabricateResidueConfidenceFromWholeProteinAverage(unittest.TestCase):
    def test_absent_residue_band_with_present_whole_protein_band_does_not_leak_into_structural_biology(self):
        # Realistic shape: `pipeline/alphafold/lookup.py`'s real
        # AlphaFold result always sets `affected_residue_band`
        # explicitly (None when the position couldn't be mapped -- see
        # `report/clinical_report_builder.py::_structural_knowledge`'s
        # own `confidence_band_is_residue_specific = residue_band is
        # not None` check, which already treats this exact key the
        # correct way). `mean_plddt_band` is the whole-protein average,
        # always present whenever AlphaFold found a structure at all.
        alphafold_result = {
            "skipped": False,
            "error": None,
            "found": True,
            "affected_residue_band": None,  # residue-specific: genuinely absent (e.g. intronic variant)
            "mean_plddt_band": "very_high",  # whole-protein average: present
        }

        score = ConfidenceEngine._structural_quality(alphafold_result, weight=1.0)

        # The whole-protein band's own value must not appear anywhere in
        # the rationale text reaching the Confidence table -- neither the
        # provider's own underscored spelling nor a space-formatted
        # rendering of it.
        self.assertNotIn("very_high", score.rationale)
        self.assertNotIn("very high", score.rationale.lower())

        # `quality_map["very_high"] == 1.0` -- the score the whole-protein
        # band alone would justify. The whole-protein value must not be
        # what drives this variant's Structural Biology quality figure.
        self.assertNotEqual(
            score.quality,
            1.0,
            "Structural Biology quality score reflects the whole-protein average confidence band "
            "('very_high') even though this variant's own residue-specific confidence is absent -- "
            "the whole-protein value reached the Confidence table.",
        )


if __name__ == "__main__":
    unittest.main()
