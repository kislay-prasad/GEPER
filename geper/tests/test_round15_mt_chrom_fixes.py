"""
Round 15: five more chrom-normalization sites turned out to share the
same missing M-family special case round 14 B1 fixed at two other
sites (`pipeline/ensembl/provider.py::_normalize_chrom`,
`pipeline/clingen/utils.py::_fetch_overlapping_genes`) -- a bare
`chrom[3:] if startswith("chr")` strip maps `"chrM"`/`"M"` input to
`"M"`, which is wrong for every target here except gnomAD's (see
`ROUND_CANDIDATES.md` for why the two gnomAD sites are intentionally
NOT fixed -- unreachable, not merely untidy).

Each site's *correct* target token differs -- that's exactly why round
15's investigation rejected a single shared `normalize_chrom(chrom,
target=...)` in favour of fixing each site in place. So this file does
NOT use one shared parametrized assertion; each test class asserts its
own site's own real expected output, mirroring
`test_mtdna_chrom_normalization.py`'s pattern.

No network, no torch/tensorflow, no subprocess -- pure-function checks
plus one call each mocked at the network boundary (matching
`test_gnomad_provider.py`'s mocking boundary) for the two sites
(`clinvar_client`, `dbsnp_client`, `thousand_genomes_sas`) where the
chrom transform lives inside a query-building method rather than a
standalone function.
"""

import unittest
from unittest import mock

from pipeline.conservation.utils import normalize_chrom as conservation_normalize_chrom
from pipeline.sequence_context import SequenceContextGenerator

_MT_SPELLINGS = ("MT", "M", "chrM", "chrMT")


class TestConservationNormalizeChrom(unittest.TestCase):
    """UCSC's own mitochondrial contig name is 'chrM', never 'chrMT'."""

    def test_all_mt_spellings_produce_chrM(self):
        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(conservation_normalize_chrom(spelling), "chrM")

    def test_nuclear_chromosomes_unaffected(self):
        cases = {"17": "chr17", "chr17": "chr17", "X": "chrX", "chrX": "chrX"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(conservation_normalize_chrom(raw), expected)


class TestClinVarEntrezChrom(unittest.TestCase):
    """Confirmed live (round 15): NCBI Entrez ClinVar's `[chr]` field
    returns hits for 'MT[chr]' and zero ("phrase not found") for
    'M[chr]'."""

    def test_all_mt_spellings_produce_MT(self):
        from database.clinvar_client import _entrez_chrom

        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(_entrez_chrom(spelling), "MT")

    def test_nuclear_chromosomes_unaffected(self):
        from database.clinvar_client import _entrez_chrom

        cases = {"17": "17", "chr17": "17", "X": "X", "chrX": "X"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_entrez_chrom(raw), expected)

    def test_positional_search_term_uses_MT_not_M(self):
        from database.clinvar_client import ClinVarClient
        from pipeline.vcf_parser import Variant

        client = ClinVarClient.__new__(ClinVarClient)  # no network/config setup needed for this pure method
        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                variant = Variant(
                    chrom=spelling, pos=8993, variant_id=".", ref="T", alt="G", qual=None, filter_status=None
                )
                term = ClinVarClient._positional_search_term(client, variant, "GRCh38")
                self.assertTrue(term.startswith("MT[chr]"), term)


class TestDbSNPEntrezChrom(unittest.TestCase):
    """Confirmed live (round 15): NCBI Entrez dbSNP's `[CHR]` field
    returns hits for 'MT[CHR]' and zero ("phrase not found") for
    'M[CHR]'."""

    def test_all_mt_spellings_produce_MT(self):
        from database.dbsnp_client import _entrez_chrom

        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(_entrez_chrom(spelling), "MT")

    def test_nuclear_chromosomes_unaffected(self):
        from database.dbsnp_client import _entrez_chrom

        cases = {"17": "17", "chr17": "17", "X": "X", "chrX": "X"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_entrez_chrom(raw), expected)


class TestSpipBuildMinimalVcf(unittest.TestCase):
    """SPiP's BSgenome.Hsapiens.UCSC.hg38 reference has no 'chrMT'
    contig, only 'chrM'. Defence in depth: SPiP's own vendored R script
    already filters chrM out of its transcriptome before SPiP runs, so
    this fix is currently unreachable -- see `build_minimal_vcf`'s own
    docstring. This test proves only that the VCF text produced has the
    right CHROM token; it is NOT proof that SPiP calls anything for MT
    variants, and must never be read as such.
    """

    def test_all_mt_spellings_produce_chrM_column(self):
        from pipeline.models.spip.loader import build_minimal_vcf

        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                vcf_text = build_minimal_vcf(spelling, 8993, "T", "G")
                data_line = vcf_text.strip().splitlines()[-1]
                self.assertEqual(data_line.split("\t")[0], "chrM")

    def test_nuclear_chromosomes_unaffected(self):
        from pipeline.models.spip.loader import build_minimal_vcf

        cases = {"17": "chr17", "chr17": "chr17", "X": "chrX", "chrX": "chrX"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                vcf_text = build_minimal_vcf(raw, 100, "A", "G")
                data_line = vcf_text.strip().splitlines()[-1]
                self.assertEqual(data_line.split("\t")[0], expected)


class TestThousandGenomesSasResolveRsid(unittest.TestCase):
    """Ensembl's REST API wants 'MT', never bare 'M' -- same convention
    round 14 already fixed at `pipeline/ensembl/provider.py` and
    `pipeline/clingen/utils.py`."""

    def test_all_mt_spellings_query_MT_region(self):
        from annotation import thousand_genomes_sas

        for spelling in _MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                with mock.patch.object(thousand_genomes_sas, "_get", return_value=[]) as mock_get:
                    thousand_genomes_sas._resolve_rsid(spelling, 8993, "T", "G")
                url = mock_get.call_args[0][0]
                self.assertIn("/overlap/region/human/MT:", url)

    def test_nuclear_chromosomes_unaffected(self):
        from annotation import thousand_genomes_sas

        cases = {"17": "17", "chr17": "17", "X": "X", "chrX": "X"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                with mock.patch.object(thousand_genomes_sas, "_get", return_value=[]) as mock_get:
                    thousand_genomes_sas._resolve_rsid(raw, 100, "A", "G")
                url = mock_get.call_args[0][0]
                self.assertIn(f"/overlap/region/human/{expected}:", url)


class TestSequenceContextNormalizeChromCaseInsensitive(unittest.TestCase):
    """Round 15: the previous exact-case ('M','mt','Mt') tuple missed
    spellings like all-lowercase 'chrm' or 'CHRM'."""

    def test_all_case_variants_of_m_produce_MT(self):
        for spelling in ("M", "m", "chrM", "chrm", "CHRM", "ChrM", "chrMT", "chrmt", "MT", "mt"):
            with self.subTest(spelling=spelling):
                self.assertEqual(SequenceContextGenerator._normalize_chrom(spelling), "MT")

    def test_nuclear_chromosomes_unaffected(self):
        cases = {"17": "17", "chr17": "17", "X": "X", "chrX": "X", "Y": "Y", "chrY": "Y"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(SequenceContextGenerator._normalize_chrom(raw), expected)


if __name__ == "__main__":
    unittest.main()
