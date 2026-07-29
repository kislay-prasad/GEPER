"""
Live-network verification for `pipeline/alphafold/provider.py`'s
`LiveAPIAlphaFoldProvider` against AlphaFold DB's real prediction API
and structure-file download (`alphafold.ebi.ac.uk`).

Every other AlphaFold test (`test_alphafold.py`, `test_alphafold_provider.py`)
mocks `requests` out, per that module's own documented sandbox caveat
("this sandbox has no network route to alphafold.ebi.ac.uk ...
re-verify against a live call before relying on it in production").
This file is that re-verification, cross-checked against real,
well-characterized structural biology rather than an arbitrary
threshold:

  - BRCA1 (P38398) has a large, well-documented intrinsically
    disordered central region flanked by two well-folded domains
    (N-terminal RING, C-terminal tandem BRCT) -- exactly the kind of
    protein where per-residue pLDDT should visibly track known fold
    quality rather than sit at one flat value.
  - Hen egg-white lysozyme (P00698) is one of the first proteins ever
    solved by X-ray crystallography and is uniformly a small, rigid,
    single-domain globular fold -- AlphaFold should score it uniformly
    high confidence, a useful contrast against BRCA1's very mixed
    profile.

Skips (rather than fails) if the live API is unreachable, so this
suite stays green in a network-restricted environment.
"""

import unittest

import requests

from pipeline.alphafold.provider import LiveAPIAlphaFoldProvider


def _skip_if_unreachable(test_case, fn):
    try:
        return fn()
    except requests.RequestException as exc:
        test_case.skipTest(f"AlphaFold DB live API unreachable in this environment: {exc}")


class TestAlphaFoldLiveBRCA1(unittest.TestCase):
    ACCESSION = "P38398"

    def setUp(self):
        self.provider = LiveAPIAlphaFoldProvider()

    def test_ring_domain_residue_is_high_confidence(self):
        # Residue 30 sits inside BRCA1's well-folded N-terminal RING
        # finger (~24-64) -- expect confident-to-very-high pLDDT.
        ann = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION, protein_position=30))
        self.assertTrue(ann.found)
        self.assertIsNotNone(ann.affected_residue_plddt)
        self.assertGreater(ann.affected_residue_plddt, 70)
        self.assertIn(ann.affected_residue_band, ("confident", "very_high"))

    def test_brct_domain_residue_is_high_confidence(self):
        # Residue 1700 sits inside the tandem BRCT repeat region
        # (~1650-1859) -- expect confident-to-very-high pLDDT.
        ann = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION, protein_position=1700))
        self.assertTrue(ann.found)
        self.assertIsNotNone(ann.affected_residue_plddt)
        self.assertGreater(ann.affected_residue_plddt, 70)
        self.assertIn(ann.affected_residue_band, ("confident", "very_high"))

    def test_central_disordered_region_residue_is_low_confidence(self):
        # Residue 800 sits well inside BRCA1's large, well-documented
        # intrinsically disordered central region (roughly aa 300-1600)
        # -- expect low-to-very-low pLDDT, the opposite of the two
        # folded-domain residues above. This is the actual
        # cross-check: real structural data pulled from a real
        # well-characterized protein should show a *contrast* between
        # its folded and disordered regions, not a single flat score.
        ann = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION, protein_position=800))
        self.assertTrue(ann.found)
        self.assertIsNotNone(ann.affected_residue_plddt)
        self.assertLess(ann.affected_residue_plddt, 55)
        self.assertIn(ann.affected_residue_band, ("low", "very_low"))

    def test_whole_protein_mean_reflects_disorder_dominance(self):
        # BRCA1's disordered region is much longer than its two folded
        # domains combined, so the whole-protein mean should be pulled
        # down into low/very-low territory -- unlike a uniformly folded
        # protein (see lysozyme below).
        ann = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION))
        self.assertTrue(ann.found)
        self.assertIsNotNone(ann.mean_plddt)
        self.assertLess(ann.mean_plddt, 60)


class TestAlphaFoldLiveLysozyme(unittest.TestCase):
    """Hen egg-white lysozyme (P00698): small, rigid, single-domain globular protein -- the contrast case against BRCA1's disorder."""

    ACCESSION = "P00698"

    def setUp(self):
        self.provider = LiveAPIAlphaFoldProvider()

    def test_uniformly_high_confidence(self):
        ann = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION, protein_position=50))
        self.assertTrue(ann.found)
        self.assertIsNotNone(ann.mean_plddt)
        self.assertGreater(ann.mean_plddt, 85)
        self.assertEqual(ann.mean_plddt_band, "very_high")
        self.assertGreater(ann.affected_residue_plddt, 85)


if __name__ == "__main__":
    unittest.main()
