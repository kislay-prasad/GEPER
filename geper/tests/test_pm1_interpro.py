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

    def test_unknown_position_is_not_evaluated_not_a_false_negative(self):
        # Regression test for the PM1 false-negative fix (2026-07-31):
        # when the transcript-verified protein position could not be
        # determined (protein_position=None), this must be
        # `not_evaluated` -- a confident `not_triggered` ("does not
        # overlap any domain") would be a wrong claim, since domain
        # overlap was never actually checked. Real accession, real
        # domain data (BRCA1 P38398 genuinely has domains), but no
        # position -- isolates the missing-position case from a
        # "found=False" case, which test_no_accession_is_not_evaluated
        # already covers separately.
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P38398"}, protein_position=None)
        )
        self.assertIsNone(result["affected_domains"])
        criterion = ACMGRuleEngine._pm1(result)
        self.assertEqual(criterion.status, "not_evaluated")
        self.assertIn("could not be determined", criterion.rationale)


class TestPM1SynonymousGate(unittest.TestCase):
    """
    Regression tests for the I6 fix: PM1 must not trigger on a
    synonymous variant, even when the residue genuinely overlaps a
    domain -- domain-overlap evidence is a claim about an *altered*
    residue, and a synonymous variant alters none.

    Real, live-verified fixture: `test_data/conflict_tiers.vcf`
    Finding 1, MLH1 (P40692) `p.Gly181=` -- residue 181 genuinely
    overlaps MLH1's histidine-kinase-like ATPase domain (confirmed live
    via `InterProLookup.query_variant`), which is exactly why the old
    code triggered PM1 for 2 points despite no amino acid change.
    """

    def setUp(self):
        self.lookup = InterProLookup()

    def test_mlh1_g181_synonymous_does_not_trigger_pm1(self):
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P40692"}, protein_position=181)
        )
        self.assertTrue(result["affected_domains"])  # genuinely overlaps a domain -- the old trigger condition
        criterion = ACMGRuleEngine._pm1(result, is_synonymous=True)
        self.assertEqual(criterion.status, "not_triggered")
        self.assertIn("synonymous", criterion.rationale.lower())
        self.assertNotIn("Histidine kinase", criterion.rationale)

    def test_mlh1_g181_still_triggers_when_missense(self):
        # Same domain-overlap fixture, but a missense call at the same
        # residue must still trigger -- this is not a blanket
        # "MLH1/domain overlap never triggers" regression.
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P40692"}, protein_position=181)
        )
        criterion = ACMGRuleEngine._pm1(result, is_synonymous=False)
        self.assertEqual(criterion.status, "triggered")

    def test_domain_count_in_supporting_evidence_matches_names_listed(self):
        # Regression test for I7: MLH1 residue 181 genuinely overlaps 4
        # `affected_domains` entries (see this class's docstring) --
        # the old code always reported `len(affected)` (4) while
        # truncating the name list to 3, so the count and the names
        # disagreed. Now the count in the text always matches what's
        # actually named, with an explicit "(showing N of TOTAL)" note
        # when truncated.
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P40692"}, protein_position=181)
        )
        self.assertEqual(len(result["affected_domains"]), 4)
        criterion = ACMGRuleEngine._pm1(result, is_synonymous=False)
        self.assertEqual(criterion.status, "triggered")
        evidence_line = criterion.supporting_evidence[0]
        stated_count = int(evidence_line.split("overlaps ")[1].split(" domain")[0])
        # The names actually shown (first 3 of the 4 affected_domains entries).
        expected_names = [
            d.get("name") or d.get("member_accession") or "unnamed domain" for d in result["affected_domains"][:3]
        ]
        self.assertEqual(stated_count, len(expected_names))
        for name in expected_names:
            self.assertIn(name, evidence_line)
        self.assertIn("(showing 3 of 4)", evidence_line)

    def test_undetermined_synonymous_status_does_not_gate(self):
        # is_synonymous=None (undetermined) must fall through to the
        # existing protein_position-based logic unchanged, not be
        # treated as "known synonymous".
        result = _skip_if_unreachable(
            self, lambda: self.lookup.query_variant(uniprot_result={"accession": "P40692"}, protein_position=181)
        )
        criterion = ACMGRuleEngine._pm1(result, is_synonymous=None)
        self.assertEqual(criterion.status, "triggered")


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
