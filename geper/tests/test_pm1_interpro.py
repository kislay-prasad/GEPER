"""
Real-data test set for PM1 (`ACMGRuleEngine._pm1` in
`pipeline/acmg_rules.py`), the one ACMG/AMP criterion that consumes
InterPro/Pfam domain evidence.

`test_interpro_live.py` already verifies the InterPro provider layer
against live data. This file verifies the next link in the chain: that
`InterProLookup.query_variant`'s `affected_domains` field, once fed a
real domain-overlap result, correctly drives `_pm1` to "triggered" or
"not_triggered" -- using the same well-documented BRCA1/TP53 domain
boundaries as the live provider tests, rather than synthetic domain
dicts. There was previously no dedicated test coverage for `_pm1` at
all (it was only exercised indirectly, if at all, by broader
orchestrator-level tests).

Uses `InterProLookup.query_variant` directly (real live network call)
so this test set doubles as an integration check of
provider -> lookup -> affected_domains -> _pm1, not just the rule
function in isolation. Skips gracefully if the live API is unreachable.
"""

import unittest

import requests

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpro.lookup import InterProLookup


def _skip_if_unreachable(test_case, fn):
    try:
        return fn()
    except requests.RequestException as exc:
        test_case.skipTest(f"InterPro live API unreachable in this environment: {exc}")


class TestPM1WithRealBRCA1Domains(unittest.TestCase):
    """BRCA1 (P38398): RING finger ~24-64, BRCT repeats ~1650-1859 (see test_interpro_live.py for the source citations)."""

    def setUp(self):
        self.lookup = InterProLookup()

    def test_residue_inside_ring_domain_triggers_pm1(self):
        # Real, well-documented BRCA1 pathogenic RING-domain variant
        # territory (e.g. C61G is a classic pathogenic RING missense).
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P38398"}, protein_position=61)
        )
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "triggered")
        self.assertEqual(criterion.strength, "moderate")
        self.assertIn("InterPro", criterion.evidence_sources)
        self.assertTrue(criterion.supporting_evidence)

    def test_residue_inside_brct_domain_triggers_pm1(self):
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P38398"}, protein_position=1700)
        )
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "triggered")

    def test_residue_outside_any_domain_does_not_trigger_pm1(self):
        # Residue 500 sits in BRCA1's large central region, well clear
        # of every domain/family/site match InterPro reports (see
        # test_interpro_live.py's full domain dump: nothing overlaps
        # ~113-1607 except the small serine-rich region at 345-508 --
        # use 900, comfortably outside every annotated region).
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P38398"}, protein_position=900)
        )
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "not_triggered")
        self.assertIn("InterPro", criterion.evidence_sources)

    def test_no_accession_is_not_evaluated(self):
        result = self.lookup.query_variant(uniprot_result={"found": False}, protein_position=61)
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "not_evaluated")


class TestPM1WithRealTP53Domains(unittest.TestCase):
    """TP53 (P04637): DNA-binding domain ~94-312 -- second independent real-data cross-check gene."""

    def setUp(self):
        self.lookup = InterProLookup()

    def test_residue_inside_dna_binding_domain_triggers_pm1(self):
        # R175 is one of TP53's best-known pathogenic mutational
        # hotspot residues, sitting inside the DNA-binding domain.
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P04637"}, protein_position=175)
        )
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "triggered")
        names_mentioned = " ".join(criterion.supporting_evidence).lower()
        self.assertTrue(any(term in names_mentioned for term in ("p53", "dna-binding", "dna binding")))


if __name__ == "__main__":
    unittest.main()
