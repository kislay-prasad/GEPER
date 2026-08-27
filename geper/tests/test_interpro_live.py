"""
Live-network verification for `pipeline/interpro/provider.py`'s
`LiveAPIInterProProvider` against InterPro's real REST API
(`www.ebi.ac.uk/interpro/api`).

Every other InterPro test (`test_interpro.py`, `test_interpro_provider.py`)
mocks `requests` out, per that module's own documented sandbox caveat
("this sandbox has no network route to ebi.ac.uk ... re-verify against
a live call before relying on it in production"). This file is that
re-verification: it makes real HTTP calls against two well-documented,
extensively-characterized genes and checks the returned domain
boundaries against their published literature values.

Skips (rather than fails) if the live API is unreachable, so this
suite stays green in a network-restricted environment -- it is meant
to be run explicitly wherever network access is available, not as a
silent no-op in CI.
"""

import unittest

import pytest
import requests

from pipeline.interpro.provider import LiveAPIInterProProvider

pytestmark = pytest.mark.live_network


def _skip_if_unreachable(test_case, fn):
    try:
        return fn()
    except requests.RequestException as exc:
        test_case.skipTest(f"InterPro live API unreachable in this environment: {exc}")


class TestInterProLiveBRCA1(unittest.TestCase):
    """
    BRCA1 (UniProt P38398): a canonical two-domain tumor suppressor.
    Published domain boundaries (UniProt feature table / InterPro
    entry IPR001357, IPR001841):
      - N-terminal RING finger (E3 ligase, with BARD1): ~aa 24-64
      - Tandem C-terminal BRCT repeats: ~aa 1650-1859
    """

    ACCESSION = "P38398"

    def setUp(self):
        self.provider = LiveAPIInterProProvider()
        self.annotation = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION))

    def test_found_with_multiple_matches(self):
        self.assertTrue(self.annotation.found)
        self.assertIsNone(self.annotation.error)
        # BRCA1 is one of the most heavily curated proteins in InterPro --
        # RING + BRCT + several superfamily/site/family entries across
        # multiple member databases.
        self.assertGreater(len(self.annotation.domains), 10)

    def test_ring_finger_domain_boundaries(self):
        ring_matches = [
            d
            for d in self.annotation.domains
            if d.name and "ring" in d.name.lower() and d.member_database == "interpro"
        ]
        self.assertTrue(ring_matches, "expected at least one InterPro-integrated RING finger entry")
        ring = ring_matches[0]
        # Published RING domain: ~24-64. Allow a small margin for
        # differing entry boundaries across InterPro's own overlapping
        # RING-related entries (finger vs. superfamily vs. profile).
        self.assertLess(ring.start, 30)
        self.assertGreater(ring.end, 55)
        self.assertLess(ring.end, 120)

    def test_brct_domain_boundaries(self):
        brct_matches = [
            d
            for d in self.annotation.domains
            if d.name and "brct" in d.name.lower() and d.member_database == "interpro"
        ]
        self.assertTrue(brct_matches, "expected at least one InterPro-integrated BRCT entry")
        brct = brct_matches[0]
        # Published tandem BRCT repeat region: ~1650-1859.
        self.assertGreater(brct.start, 1550)
        self.assertLess(brct.start, 1700)
        self.assertGreater(brct.end, 1800)

    def test_pfam_member_database_entries_present(self):
        # GEPER's spec explicitly calls out Pfam calls -- confirm the
        # live response actually carries Pfam-sourced matches (not just
        # InterPro-integrated ones), matching parse_interpro_response's
        # "surfaces both" behavior.
        pfam_matches = [d for d in self.annotation.domains if d.member_database == "pfam"]
        self.assertTrue(pfam_matches)
        pfam_accessions = {d.member_accession for d in pfam_matches}
        self.assertIn("PF00533", pfam_accessions)  # BRCA1 C Terminus (BRCT) domain
        self.assertIn("PF00097", pfam_accessions)  # Zinc finger, C3HC4 type (RING finger)


class TestInterProLiveTP53(unittest.TestCase):
    """
    TP53 (UniProt P04637): the other classic well-characterized
    multi-domain protein, used here as a second independent
    cross-check gene. Published domains (InterPro IPR011615, IPR010991):
      - DNA-binding domain: ~aa 94-312
      - Tetramerization domain: ~aa 319-360
    """

    ACCESSION = "P04637"

    def setUp(self):
        self.provider = LiveAPIInterProProvider()
        self.annotation = _skip_if_unreachable(self, lambda: self.provider.query(self.ACCESSION))

    def test_dna_binding_domain_boundaries(self):
        self.assertTrue(self.annotation.found)
        dbd_matches = [
            d
            for d in self.annotation.domains
            if d.name and "dna-binding" in d.name.lower() and d.member_database == "interpro"
        ]
        self.assertTrue(dbd_matches, "expected an InterPro-integrated p53 DNA-binding domain entry")
        dbd = dbd_matches[0]
        self.assertGreater(dbd.start, 80)
        self.assertLess(dbd.start, 120)
        self.assertGreater(dbd.end, 270)
        self.assertLess(dbd.end, 320)

    def test_tetramerization_domain_boundaries(self):
        tetra_matches = [
            d
            for d in self.annotation.domains
            if d.name and "tetrameri" in d.name.lower() and d.member_database == "interpro"
        ]
        self.assertTrue(tetra_matches, "expected an InterPro-integrated p53 tetramerization domain entry")
        tetra = tetra_matches[0]
        self.assertGreater(tetra.start, 300)
        self.assertLess(tetra.end, 380)


if __name__ == "__main__":
    unittest.main()
