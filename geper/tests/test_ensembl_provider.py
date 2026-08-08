"""
Ground-truth tests for `pipeline/ensembl/provider.py::LocalDatasetEnsemblProvider`,
built from real Ensembl data rather than synthetic fixtures -- matching
the split `tests/test_mane_bootstrap.py`/`tests/test_mane_provider.py`
already established (mechanics in the bootstrap test, real-data
verification here).

`tests/fixtures/ensembl_gtf_brca1_tp53_stk11_cbarp.tsv` is a verbatim
excerpt of Ensembl's real human GTF (release 116, fetched 2026-08-08
from https://ftp.ensembl.org/pub/current/gtf/homo_sapiens/) -- every
`gene`/`transcript`/`exon`/`CDS`/`stop_codon`/UTR line for BRCA1's own
canonical transcript (`ENST00000357654`, 23 exons), TP53's own canonical
transcript (`ENST00000269305`, 11 exons), and the bare `gene` lines for
STK11/CBARP -- the same real overlapping-gene pair already used as
ground truth in `tests/test_clingen_gene_resolution.py` (GRCh38
19:1,177,558-1,239,465; STK11 +strand 1,177,558-1,228,431, CBARP
-strand 1,228,282-1,239,465). `tests/fixtures/ensembl_cds_brca1_tp53.fa`
is the matching real CDS FASTA records for both canonical transcripts.

Known real facts checked here: BRCA1's canonical transcript is
`ENST00000357654`, tagged both `MANE_Select` and `Ensembl_canonical`,
1863 aa (5592nt CDS FASTA, stop-inclusive); TP53's canonical transcript
is `ENST00000269305`, 393 aa (1182nt CDS FASTA); a position inside
STK11's real last coding exon (19:1,228,350) overlaps both STK11 and
CBARP.
"""

import os
import unittest
from unittest import mock

from pipeline.ensembl import bootstrap as ensembl_bootstrap
from pipeline.ensembl.provider import LocalDatasetEnsemblProvider

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_GTF_FIXTURE = os.path.join(_FIXTURES_DIR, "ensembl_gtf_brca1_tp53_stk11_cbarp.tsv")
_CDS_FIXTURE = os.path.join(_FIXTURES_DIR, "ensembl_cds_brca1_tp53.fa")

_STK11_POS = 1228350  # inside STK11's real last coding exon, and CBARP's real first exon (opposite strand)


def _build_real_dataset(dest_path: str) -> None:
    """Runs the real bootstrap parse+merge pipeline (no network) over the
    live-extracted GTF/CDS fixtures and writes the resulting JSON-lines
    dataset to `dest_path`, exactly as `_build_dataset` would from a real
    download -- so this test exercises the true end-to-end parsing path,
    not a hand-written stand-in for its output."""
    with open(_GTF_FIXTURE, "r", encoding="utf-8") as fh:
        genes = ensembl_bootstrap._parse_gtf_lines(fh)
    wanted_ids = {e["transcript"]["transcript_id"] for e in genes.values() if e["transcript"]}
    with open(_CDS_FIXTURE, "r", encoding="utf-8") as fh:
        sequences = ensembl_bootstrap._parse_cds_fasta_lines(fh, wanted_ids)
    for entry in genes.values():
        if entry["transcript"]:
            entry["transcript"]["cds_sequence"] = sequences.get(entry["transcript"]["transcript_id"])

    import json

    with open(dest_path, "w", encoding="utf-8") as fh:
        for gene_symbol, entry in sorted(genes.items()):
            fh.write(json.dumps(entry) + "\n")


def _provider(dataset_path: str) -> LocalDatasetEnsemblProvider:
    return LocalDatasetEnsemblProvider(local_file_path=dataset_path, auto_fetch=False)


class _RealDatasetTestCase(unittest.TestCase):
    """Builds the real dataset once per test class into a temp file."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._tmpdir = tempfile.mkdtemp()
        cls._dataset_path = os.path.join(cls._tmpdir, "ensembl_transcripts.jsonl")
        _build_real_dataset(cls._dataset_path)

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls._tmpdir, ignore_errors=True)


class TestBrca1RealGroundTruth(_RealDatasetTestCase):
    def test_resolves_the_real_canonical_transcript_id(self):
        provider = _provider(self._dataset_path)
        transcript = provider.transcript_for_gene("BRCA1")
        self.assertIsNotNone(transcript)
        self.assertEqual(transcript["transcript_id"], "ENST00000357654")

    def test_flags_match_live_ensembl(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("BRCA1")
        self.assertTrue(transcript["is_mane_select"])
        self.assertTrue(transcript["is_canonical"])

    def test_protein_length_matches_the_well_known_brca1_length(self):
        # 5592nt real CDS FASTA (stop-inclusive) = 1864 codons = 1863 aa.
        transcript = _provider(self._dataset_path).transcript_for_gene("BRCA1")
        self.assertEqual(transcript["protein_length"], 1863)

    def test_exon_count_matches_real_gencode_annotation(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("BRCA1")
        self.assertEqual(len(transcript["exons"]), 23)

    def test_cds_sequence_is_the_real_stop_inclusive_sequence(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("BRCA1")
        self.assertEqual(len(transcript["cds_sequence"]), 5592)
        self.assertTrue(transcript["cds_sequence"].startswith("ATG"))
        self.assertTrue(transcript["cds_sequence"].endswith("TGA"))

    def test_chrom_and_strand_are_real(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("BRCA1")
        self.assertEqual(transcript["chrom"], "17")
        self.assertEqual(transcript["strand"], -1)

    def test_case_insensitive_gene_symbol_lookup(self):
        provider = _provider(self._dataset_path)
        self.assertEqual(provider.transcript_for_gene("brca1")["transcript_id"], "ENST00000357654")


class TestTp53RealGroundTruth(_RealDatasetTestCase):
    def test_resolves_the_real_canonical_transcript_id(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("TP53")
        self.assertEqual(transcript["transcript_id"], "ENST00000269305")

    def test_protein_length_matches_the_well_known_tp53_length(self):
        # 1182nt real CDS FASTA (stop-inclusive) = 394 codons = 393 aa.
        transcript = _provider(self._dataset_path).transcript_for_gene("TP53")
        self.assertEqual(transcript["protein_length"], 393)

    def test_exon_count_matches_real_gencode_annotation(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("TP53")
        self.assertEqual(len(transcript["exons"]), 11)

    def test_is_mane_select_and_canonical(self):
        transcript = _provider(self._dataset_path).transcript_for_gene("TP53")
        self.assertTrue(transcript["is_mane_select"])
        self.assertTrue(transcript["is_canonical"])


class TestGeneOverlapRealGroundTruth(_RealDatasetTestCase):
    """STK11/CBARP: the same real, live-confirmed overlapping-gene pair
    `tests/test_clingen_gene_resolution.py` already uses -- a query at
    19:1,228,350 (inside STK11's last exon) must return both, matching
    what a live Ensembl `overlap/region` call reports."""

    def test_position_inside_both_genes_returns_both(self):
        provider = _provider(self._dataset_path)
        matches = provider.genes_overlapping("19", _STK11_POS)
        symbols = {m["external_name"] for m in matches}
        self.assertEqual(symbols, {"STK11", "CBARP"})

    def test_chr_prefix_is_normalized(self):
        provider = _provider(self._dataset_path)
        matches = provider.genes_overlapping("chr19", _STK11_POS)
        symbols = {m["external_name"] for m in matches}
        self.assertEqual(symbols, {"STK11", "CBARP"})

    def test_position_outside_both_genes_returns_empty_not_none(self):
        provider = _provider(self._dataset_path)
        self.assertEqual(provider.genes_overlapping("19", 1), [])

    def test_position_on_a_chromosome_not_in_the_dataset_returns_empty(self):
        provider = _provider(self._dataset_path)
        self.assertEqual(provider.genes_overlapping("99", 100), [])

    def test_feature_shape_matches_the_live_overlap_region_response(self):
        provider = _provider(self._dataset_path)
        matches = provider.genes_overlapping("19", _STK11_POS)
        for match in matches:
            self.assertEqual(match["biotype"], "protein_coding")
            self.assertIn("external_name", match)


class TestAvailability(unittest.TestCase):
    def test_unavailable_when_ensembl_disabled(self):
        fake_config = mock.Mock()
        fake_config.ensembl.ENABLED = False
        fake_config.ensembl.LOCAL_FILE = ""
        with mock.patch("pipeline.ensembl.provider.CONFIG", fake_config):
            provider = LocalDatasetEnsemblProvider(local_file_path=_GTF_FIXTURE, auto_fetch=False)
            self.assertFalse(provider.is_available())
            self.assertIsNone(provider.transcript_for_gene("BRCA1"))
            self.assertIsNone(provider.genes_overlapping("17", 43100000))

    def test_unavailable_without_local_file_or_auto_fetch(self):
        provider = LocalDatasetEnsemblProvider(local_file_path=None, auto_fetch=False)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.transcript_for_gene("BRCA1"))

    def test_missing_local_file_degrades_to_empty_not_a_crash(self):
        provider = LocalDatasetEnsemblProvider(local_file_path="/does/not/exist.jsonl", auto_fetch=False)
        self.assertIsNone(provider.transcript_for_gene("BRCA1"))
        self.assertEqual(provider.genes_overlapping("17", 43100000), [])


class TestAutoFetchNeverTriggeredWithExplicitPath(_RealDatasetTestCase):
    def test_bootstrap_module_never_touched_when_local_file_given(self):
        with mock.patch("pipeline.ensembl.bootstrap.ensure_dataset_file") as fake_ensure:
            provider = LocalDatasetEnsemblProvider(local_file_path=self._dataset_path)
            provider.transcript_for_gene("BRCA1")
        fake_ensure.assert_not_called()


class TestLoadedOnlyOnce(_RealDatasetTestCase):
    def test_file_is_parsed_once_across_repeated_lookups(self):
        provider = _provider(self._dataset_path)
        with mock.patch.object(provider, "_load", wraps=provider._load) as spy_load:
            provider.transcript_for_gene("BRCA1")
            provider.transcript_for_gene("TP53")
            provider.genes_overlapping("19", _STK11_POS)
        spy_load.assert_called_once()


class TestModuleLevelConvenienceFunctions(_RealDatasetTestCase):
    def setUp(self):
        import pipeline.ensembl.provider as provider_module

        self._provider_module = provider_module
        self._original_default = provider_module._default_provider
        self.addCleanup(setattr, provider_module, "_default_provider", self._original_default)

    def test_uses_configured_local_file(self):
        fake_config = mock.Mock()
        fake_config.ensembl.ENABLED = True
        fake_config.ensembl.LOCAL_FILE = self._dataset_path
        with mock.patch("pipeline.ensembl.provider.CONFIG", fake_config):
            self._provider_module._default_provider = None
            from pipeline.ensembl.provider import transcript_for_gene

            self.assertEqual(transcript_for_gene("BRCA1")["transcript_id"], "ENST00000357654")


if __name__ == "__main__":
    unittest.main()
