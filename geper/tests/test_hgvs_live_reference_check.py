"""
Live-network verification that `pipeline/hgvs_utils.py::validate_hgvs`'s
reference-base cross-check works against real reference-genome data,
fetched live via `pipeline/sequence_context.py::SequenceContextGenerator
.fetch_reference_sequence` (the same Ensembl-backed source
`pipeline/orchestrator.py` wires in for production normalization/HGVS
validation -- see `_run_normalization_stage`).

Uses the same real, ClinVar-verified BRCA1 c.181T>G (GRCh38
chr17:43,106,487, ClinVar record 17661, Pathogenic) coordinate the
rest of this test suite uses -- IMPORTANT strand note, itself found by
this test the first time it ran against real data: BRCA1 is
minus-strand (confirmed live via NCBI Gene esummary for Gene ID 672:
chrstart 43,170,326 > chrstop 43,044,294, the convention indicating a
minus-strand gene). "c.181T>G" is stated in TRANSCRIPT orientation;
the real genomic-forward-strand (VCF-style) allele at this position is
therefore the reverse complement, A>C, not T>G. Skips (rather than
fails) if the live API is unreachable, matching every other "_live"
test in this project (test_interpro_live.py, test_alphafold_live.py,
test_orphanet_live_fetch.py).

EXCLUDED FROM THE DEFAULT CI RUN -- marked `live_network` (see `pytest.ini`) and
excluded there via `addopts`. This module makes real HTTP calls to Ensembl's REST
API; an Ensembl outage was making `master` go red at random intervals, which
erodes CI's signal ("red but probably external" becomes the default reading and a
real regression gets waved through the same way). Default CI green therefore does
NOT mean this live reference-base cross-check passed -- that specific coverage
(validate_hgvs's reference-base check against a REAL fetched genome base, as
opposed to a hardcoded/mocked one) is real but is not exercised by the default
run. Run it explicitly with `pytest -m live_network` when that needs confirming
against the real API.
"""

import unittest

import pytest
import requests

from pipeline.hgvs_utils import validate_hgvs
from pipeline.sequence_context import SequenceContextGenerator

pytestmark = pytest.mark.live_network


def _skip_if_unreachable(test_case, fn):
    try:
        return fn()
    except requests.RequestException as exc:
        test_case.skipTest(f"Ensembl live API unreachable in this environment: {exc}")


class TestHgvsValidationAgainstRealEnsemblReference(unittest.TestCase):
    def setUp(self):
        self.gen = SequenceContextGenerator(species="human", assembly="GRCh38")

    def _fetch_base(self, chrom: str, pos: int) -> str:
        return self.gen.fetch_reference_sequence(chrom, pos, pos)

    def test_real_brca1_correct_reference_base_validates(self):
        # Genomic-forward-strand (VCF-style) allele: A>C -- the reverse
        # complement of transcript-oriented c.181T>G, since BRCA1 is
        # minus-strand (see module docstring).
        real_base = _skip_if_unreachable(self, lambda: self._fetch_base("17", 43106487))
        self.assertEqual(real_base.upper(), "A")

        result = validate_hgvs("NC_000017.11:g.43106487A>C", fetch_reference_base=self._fetch_base)
        self.assertTrue(result.is_valid, msg=result.errors)

    def test_wrong_reference_base_against_real_genome_is_caught(self):
        _skip_if_unreachable(self, lambda: self._fetch_base("17", 43106487))  # confirm reachability first
        # 'T' is the transcript-oriented ref (c.181T>G) -- correct for
        # c. notation, but WRONG if asserted as the genomic g. reference
        # base, exactly the strand-confusion mistake this cross-check exists to catch.
        result = validate_hgvs("NC_000017.11:g.43106487T>G", fetch_reference_base=self._fetch_base)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("real reference genome has 'A'" in e for e in result.errors), msg=result.errors)


if __name__ == "__main__":
    unittest.main()
