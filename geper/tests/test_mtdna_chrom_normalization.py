"""
Round 14, B1: `pipeline/ensembl/provider.py::_normalize_chrom` and
`pipeline/clingen/utils.py::_fetch_overlapping_genes` each used to do
their own bare `chrom[3:] if startswith("chr")` prefix strip, which
mapped both `"chrM"` and bare `"M"` to `"M"` -- neither of which
matches Ensembl's own GTF/REST seqname for the mitochondrial contig,
`"MT"`. `"MT"`-spelled input happened to pass through unchanged and
work by coincidence -- which is exactly why
`test_data/nuclear_test_with_mt.vcf` (CHROM=MT literally) never
exposed this bug: it dodges it by construction, not because the code
was correct. These tests cover all four spellings this codebase is
known to encounter (`MT`, `M`, `chrM`, `chrMT` -- see
`hgvs_utils.py::is_mitochondrial_chrom`'s own docstring for that list),
not just the one the shipped fixture happens to use.

Both call sites now reuse `hgvs_utils.py::_strip_chr` (imported locally
to avoid a real import cycle -- see `_normalize_chrom`'s own docstring)
instead of duplicating the prefix-strip, so there is exactly one place
this normalization can drift.
"""

import unittest
from unittest import mock

from pipeline.clingen.utils import _fetch_overlapping_genes
from pipeline.ensembl.provider import LocalDatasetEnsemblProvider, _normalize_chrom

_MT_SPELLINGS = ("MT", "M", "chrM", "chrMT", "mt", "Mt")


class TestEnsemblProviderNormalizeChrom(unittest.TestCase):
    def test_all_mt_spellings_normalize_identically(self):
        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(_normalize_chrom(spelling), "MT")

    def test_nuclear_chromosomes_unaffected(self):
        cases = {
            "17": "17",
            "chr17": "17",
            "X": "X",
            "chrX": "X",
            "Y": "Y",
            "chrY": "Y",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_chrom(raw), expected)

    def test_none_and_empty_do_not_raise(self):
        self.assertEqual(_normalize_chrom(None), "")
        self.assertEqual(_normalize_chrom(""), "")
        self.assertEqual(_normalize_chrom("   "), "")


def _mt_gene_row():
    """One synthetic gene record in the exact shape
    `pipeline/ensembl/bootstrap.py::_choose_and_serialize` writes --
    real MT-ND1 coordinates (GRCh38, confirmed live via Ensembl REST
    this round: gene span 3307-4262, canonical transcript
    ENST00000361390), used here only to prove the chrom KEY the
    dataset is indexed under resolves identically for all four query
    spellings -- not a re-verification of the coordinates themselves
    (see tests/test_mtdna_transcript_resolution.py for that)."""
    return {
        "gene_symbol": "MT-ND1",
        "chrom": "MT",
        "gene_start": 3307,
        "gene_end": 4262,
        "strand": 1,
        "transcript": None,
    }


class TestGenesOverlappingAllMtSpellingsResolveIdentically(unittest.TestCase):
    """`_normalize_chrom` is the single seam every `genes_overlapping`
    call passes through -- this proves the full lookup path, not just
    the normalizer function in isolation, is spelling-invariant."""

    @classmethod
    def setUpClass(cls):
        import json
        import os
        import tempfile

        fd, cls._dataset_path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_mt_gene_row()) + "\n")

    @classmethod
    def tearDownClass(cls):
        import os

        os.unlink(cls._dataset_path)

    def _provider(self):
        return LocalDatasetEnsemblProvider(local_file_path=self._dataset_path, auto_fetch=False)

    def test_all_four_spellings_find_the_same_gene(self):
        for spelling in ("MT", "M", "chrM", "chrMT"):
            with self.subTest(spelling=spelling):
                matches = self._provider().genes_overlapping(spelling, 3500)
                self.assertIsNotNone(matches)
                symbols = {m["external_name"] for m in matches}
                self.assertEqual(symbols, {"MT-ND1"})


class TestClinGenFetchOverlappingGenesMtSpellings(unittest.TestCase):
    """`_fetch_overlapping_genes` builds the live Ensembl
    `overlap/region` URL directly (it is the fallback used when a VCF
    record carries no `GENE=` INFO field) -- must construct the same
    `.../MT:pos-pos` URL regardless of which spelling the caller
    passed in, never `.../M:pos-pos` (a region Ensembl's REST API does
    not recognize)."""

    def test_all_four_spellings_build_the_same_mt_url(self):
        for spelling in ("MT", "M", "chrM", "chrMT"):
            with self.subTest(spelling=spelling):
                fake_response = mock.Mock()
                fake_response.json.return_value = []
                fake_response.raise_for_status.return_value = None
                with mock.patch("pipeline.clingen.utils.requests.get", return_value=fake_response) as fake_get:
                    with mock.patch("pipeline.clingen.utils.HEALTH.is_offline", return_value=False):
                        _fetch_overlapping_genes(spelling, 3500, "GRCh38")
                called_url = fake_get.call_args[0][0]
                self.assertIn("/MT:3500-3500", called_url)
                self.assertNotIn("/M:3500-3500", called_url)


if __name__ == "__main__":
    unittest.main()
