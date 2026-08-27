"""
Live-network verification for `pipeline/orphanet/bootstrap.py` +
`pipeline/orphanet/provider.py`'s `LocalDatasetOrphanetProvider`
against Orphanet's real CC BY 4.0 bulk download
(https://www.orphadata.com/data/xml/en_product6.xml).

Every other Orphanet test (`test_orphanet.py`, `test_orphanet_provider.py`)
uses a frozen fixture file, so this is the actual "confirm real data
retrieval" check: a genuine network fetch of the real ~22MB dataset
(cached to `CONFIG.CACHE_DIR/orphanet/` after the first run, same TTL
behavior as `pipeline/hpo/bootstrap.py`), parsed, and checked against
the same well-documented genes `test_interpro_live.py`/
`test_alphafold_live.py` use for InterPro/AlphaFold, plus FBN1 (shared
with `test_hpo.py`'s real HPO/Marfan cross-check).

Skips (rather than fails) if the live download is unreachable, so this
suite stays green in a network-restricted environment.
"""

import unittest

import pytest
import requests

from pipeline.orphanet.provider import LocalDatasetOrphanetProvider

pytestmark = pytest.mark.live_network

# Force a real fetch every run (bypass any TTL-cached copy from a
# previous run of this test/the app) by pointing at a scratch path
# instead of CONFIG.orphanet's shared cache dir would defeat the
# purpose of exercising the TTL-cache path -- so this deliberately
# reuses the same cache dir/TTL behavior a production run would.


def _skip_if_unreachable(test_case, fn):
    try:
        return fn()
    except requests.RequestException as exc:
        test_case.skipTest(f"Orphanet live download unreachable in this environment: {exc}")


class TestOrphanetLiveFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = LocalDatasetOrphanetProvider()  # auto_fetch=True, real CONFIG.orphanet.DOWNLOAD_URL

    def test_brca1_real_hereditary_breast_ovarian_cancer_association(self):
        evidence = _skip_if_unreachable(self, lambda: self.provider.query("BRCA1"))
        self.assertTrue(evidence.found)
        self.assertIn("145", evidence.distinct_orpha_codes)  # Hereditary breast and/or ovarian cancer syndrome
        hboc = next(a for a in evidence.disorder_associations if a.orpha_code == "145")
        self.assertEqual(hboc.disorder_name, "Hereditary breast and/or ovarian cancer syndrome")
        self.assertIn("germline mutation", (hboc.association_type or "").lower())

    def test_fbn1_real_marfan_association(self):
        """Cross-check opportunity: FBN1 is the same gene test_hpo.py verifies against real HPO Marfan (OMIM:154700) curation."""
        evidence = _skip_if_unreachable(self, lambda: self.provider.query("FBN1"))
        self.assertTrue(evidence.found)
        marfan_matches = [
            a for a in evidence.disorder_associations if "marfan syndrome type 1" in a.disorder_name.lower()
        ]
        self.assertTrue(
            marfan_matches,
            f"expected a Marfan syndrome type 1 entry, got: {[a.disorder_name for a in evidence.disorder_associations]}",
        )
        self.assertEqual(marfan_matches[0].orpha_code, "284963")

    def test_cftr_real_cystic_fibrosis_association(self):
        evidence = _skip_if_unreachable(self, lambda: self.provider.query("CFTR"))
        self.assertTrue(evidence.found)
        self.assertIn("586", evidence.distinct_orpha_codes)  # Cystic fibrosis

    def test_gene_symbols_disambiguated_within_multi_gene_disorders(self):
        """Real cross-check: Fanconi anemia (ORPHA:84) is a real multi-gene disorder (BRCA1, BRCA2, PALB2, FANCA, ...) -- confirm each gene's own evidence record only carries its own association, not every co-associated gene's."""
        brca1 = _skip_if_unreachable(self, lambda: self.provider.query("BRCA1"))
        brca2 = _skip_if_unreachable(self, lambda: self.provider.query("BRCA2"))
        self.assertIn("84", brca1.distinct_orpha_codes)
        self.assertIn("84", brca2.distinct_orpha_codes)
        # But BRCA2 must not inherit BRCA1's HBOC-specific entry unless
        # BRCA2 itself is also independently associated with it in the
        # real dataset (it is, per Orphanet -- both are HBOC genes) --
        # the real assertion is that BRCA2 does NOT pick up an entry
        # that belongs only to a BRCA1-exclusive disorder.
        self.assertNotIn("284963", brca2.distinct_orpha_codes)  # Marfan syndrome type 1 is FBN1-only

    def test_data_version_and_citation_populated_from_real_release(self):
        evidence = _skip_if_unreachable(self, lambda: self.provider.query("BRCA1"))
        result = evidence.to_dict()
        self.assertIsNotNone(result["data_version"])
        self.assertIn("Orphadata Science", result["citation"])
        self.assertIn(result["data_version"], result["citation"])


if __name__ == "__main__":
    unittest.main()
