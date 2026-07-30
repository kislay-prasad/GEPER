"""
Tests for `pipeline/pvs1/utils.py::canonical_protein_position` -- the
transcript-verified replacement for the old
`orchestrator._estimate_protein_position` local-translation-window
heuristic (removed 2026-07-31; see `geper/test_data/README.md`'s
"Major finding" section for the empirical evidence that heuristic was
wrong for almost every real variant, and PM1's docstring in
`pipeline/acmg_rules.py` for how the fix is consumed).

Ground truth, not synthetic guesses: `tests/fixtures/pvs1_transcripts.json`
already holds the real, live-fetched Ensembl MANE Select structure for
BRCA1 (`ENST00000357654`, strand -1, `is_canonical`/`is_mane_select`
both True). The expected residue numbers below were independently
cross-checked live against Ensembl's own VEP endpoint
(`vep/human/region/{region}/{allele}` -> `transcript_consequences[].
protein_start`) on 2026-07-31, not just asserted from this fixture --
`codon_at`'s arithmetic and VEP's own transcript model agree.
"""

import json
import os
import unittest

from pipeline.pvs1.utils import canonical_protein_position, transcript_context_from_dict

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pvs1_transcripts.json")

with open(_FIXTURE, "r", encoding="utf-8") as _fh:
    _TRANSCRIPTS = json.load(_fh)["transcripts"]


def _transcript_result(gene: str, **overrides):
    record = dict(_TRANSCRIPTS[gene])
    record.update(overrides)
    return {"skipped": False, "found": True, "gene_symbol": gene, "source": "fixture", "transcript": record}


class TestCanonicalProteinPositionRealBRCA1(unittest.TestCase):
    """BRCA1 (NM_007294.4 / ENST00000357654, minus-strand) -- the gene whose
    protein position this fix was built to get right (see the module
    docstring)."""

    def test_c191_matches_cys64tyr(self):
        # NC_000017.11:g.43106477C>T, NM_007294.4:c.191G>A (p.Cys64Tyr),
        # real ClinGen ERepo PS3:Met pathogenic call, RING domain
        # residue. VEP protein_start=64 for this exact transcript,
        # confirmed live 2026-07-31.
        result = canonical_protein_position(_transcript_result("BRCA1"), 43106477)
        self.assertEqual(result, 64)

    def test_c5090_matches_cys1697tyr(self):
        # NC_000017.11:g.43063936C>T, NM_007294.4:c.5090G>A
        # (p.Cys1697Tyr), real ClinGen ERepo PS3:Met call, BRCT-repeat
        # residue -- 5000+ nt into the CDS, in a different exon than
        # the transcript's own start codon (exactly the case the old
        # unspliced-window heuristic got wrong).
        result = canonical_protein_position(_transcript_result("BRCA1"), 43063936)
        self.assertEqual(result, 1697)

    def test_c1233_matches_asp411glu(self):
        # NC_000017.11:g.43094298A>C, NM_007294.4:c.1233T>G
        # (p.Asp411Glu), real ClinGen ERepo BS3:Met benign call.
        result = canonical_protein_position(_transcript_result("BRCA1"), 43094298)
        self.assertEqual(result, 411)

    def test_intronic_position_is_none(self):
        # NC_000017.11:g.43104861 is a real intronic position (BRCA1
        # c.301+7G>A) -- outside every coding span, so codon_at itself
        # returns None; this must not be papered over with a guess.
        result = canonical_protein_position(_transcript_result("BRCA1"), 43104861)
        self.assertIsNone(result)


class TestCanonicalProteinPositionHonestFailureModes(unittest.TestCase):
    """Every input shape that must return None -- never a guessed position."""

    def test_no_transcript_result(self):
        self.assertIsNone(canonical_protein_position(None, 43106477))

    def test_skipped_transcript_lookup(self):
        self.assertIsNone(canonical_protein_position({"skipped": True}, 43106477))

    def test_errored_transcript_lookup(self):
        self.assertIsNone(canonical_protein_position({"skipped": False, "error": "boom"}, 43106477))

    def test_not_found(self):
        self.assertIsNone(canonical_protein_position({"skipped": False, "found": False}, 43106477))

    def test_variant_outside_fetched_transcript(self):
        result = _transcript_result("BRCA1")
        result["variant_outside_transcript"] = True
        self.assertIsNone(canonical_protein_position(result, 43106477))

    def test_non_canonical_non_mane_transcript_is_rejected(self):
        # Matches `functional_regions_from_interpro`'s identical guard:
        # InterPro/UniProt coordinates are only meaningful against the
        # canonical isoform, so a position from any other transcript
        # must not be trusted for domain-overlap comparison, even
        # though `codon_at` itself would happily compute one.
        result = _transcript_result("BRCA1", is_canonical=False, is_mane_select=False)
        self.assertIsNone(canonical_protein_position(result, 43106477))

    def test_mane_select_without_canonical_flag_is_accepted(self):
        result = _transcript_result("BRCA1", is_canonical=False, is_mane_select=True)
        self.assertEqual(canonical_protein_position(result, 43106477), 64)

    def test_canonical_without_mane_select_flag_is_accepted(self):
        result = _transcript_result("BRCA1", is_canonical=True, is_mane_select=False)
        self.assertEqual(canonical_protein_position(result, 43106477), 64)


if __name__ == "__main__":
    unittest.main()
