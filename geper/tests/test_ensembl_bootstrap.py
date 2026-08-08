"""
Tests for `pipeline/ensembl/bootstrap.py` -- the self-provisioning
fetch+parse+convert of Ensembl's human GTF + CDS FASTA downloads into
the gene-symbol-indexed JSON-lines shape
`pipeline/ensembl/provider.py::LocalDatasetEnsemblProvider` expects.
`requests.get` is mocked throughout -- no live network calls here.

The GTF/FASTA attribute shapes below are real, live-confirmed formats
(fetched 2026-08-08 against
https://ftp.ensembl.org/pub/current/gtf/homo_sapiens/ and
https://ftp.ensembl.org/pub/current_fasta/homo_sapiens/cds/), but the
coordinates and sequences are a small, hand-built, internally-consistent
synthetic transcript -- exercising the parsing/choice/merge mechanics in
isolation. Ground-truth verification against a real, live-extracted
BRCA1/TP53/STK11/CBARP excerpt lives in `tests/test_ensembl_provider.py`
instead, matching the split `tests/test_mane_bootstrap.py`/
`tests/test_mane_provider.py` already established.
"""

import gzip
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

from pipeline.ensembl import bootstrap as ensembl_bootstrap

_GTF_INDEX_HTML_EXCERPT = """
<html>
<head><title>Index of /pub/current/gtf/homo_sapiens/</title></head>
<body>
<pre>Name                                                          Last modified      Size
<a href="CHECKSUMS">CHECKSUMS</a>                                                    2026-08-07 00:00   61
<a href="Homo_sapiens.GRCh38.116.abinitio.gtf.gz">Homo_sapiens.GRCh38.116.abinitio.gtf.gz</a>                     2026-03-24 20:10   12M
<a href="Homo_sapiens.GRCh38.116.chr.gtf.gz">Homo_sapiens.GRCh38.116.chr.gtf.gz</a>   2026-03-24 20:10   50M
<a href="Homo_sapiens.GRCh38.116.chr_patch_hapl_scaff.gtf.gz">Homo_sapiens.GRCh38.116.chr_patch_hapl_scaff.gtf.gz</a>          2026-03-24 20:10   51M
<a href="Homo_sapiens.GRCh38.116.gtf.gz">Homo_sapiens.GRCh38.116.gtf.gz</a>                               2026-03-24 20:10   50M
<a href="README">README</a>                                                       2026-03-24 20:10  1.8K
</pre>
</body></html>
"""

_INDEX_URL = "https://ftp.ensembl.org/pub/current/gtf/homo_sapiens/"

# A small, internally-consistent synthetic gene "TESTG1" on chr7,
# minus strand, with two protein-coding transcripts -- one plain, one
# tagged `Ensembl_canonical`/`MANE_Select` -- to exercise the
# canonical-transcript-choice logic. Real GTF attribute-string shape,
# confirmed live against BRCA1's own canonical transcript record.
_GTF_BODY = "\n".join(
    [
        '7\tensembl_havana\tgene\t1000\t2000\t.\t-\t.\tgene_id "ENSG00000000001"; gene_name "TESTG1"; gene_biotype "protein_coding";',
        '7\thavana\ttranscript\t1000\t2000\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000001"; gene_name "TESTG1"; transcript_biotype "protein_coding"; tag "gencode_basic";',
        '7\thavana\texon\t1900\t2000\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000001"; exon_number "1";',
        '7\thavana\texon\t1000\t1200\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000001"; exon_number "2";',
        '7\thavana\tCDS\t1000\t1200\t.\t-\t0\tgene_id "ENSG00000000001"; transcript_id "ENST00000000001";',
        '7\tensembl_havana\ttranscript\t1000\t2000\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000002"; gene_name "TESTG1"; transcript_biotype "protein_coding"; tag "CCDS"; tag "gencode_basic"; tag "gencode_primary"; tag "MANE_Select"; tag "Ensembl_canonical";',
        '7\tensembl_havana\texon\t1900\t2000\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000002"; exon_number "1";',
        '7\tensembl_havana\texon\t1000\t1206\t.\t-\t.\tgene_id "ENSG00000000001"; transcript_id "ENST00000000002"; exon_number "2";',
        '7\tensembl_havana\tCDS\t1003\t1206\t.\t-\t0\tgene_id "ENSG00000000001"; transcript_id "ENST00000000002"; protein_id "ENSP00000000002";',
        '7\tensembl_havana\tstop_codon\t1000\t1002\t.\t-\t0\tgene_id "ENSG00000000001"; transcript_id "ENST00000000002";',
        # A second, non-protein-coding gene right after -- must not pollute TESTG1's block.
        '7\thavana\tgene\t5000\t5500\t.\t+\t.\tgene_id "ENSG00000000003"; gene_name "TESTLNC"; gene_biotype "lncRNA";',
        '7\thavana\ttranscript\t5000\t5500\t.\t+\t.\tgene_id "ENSG00000000003"; transcript_id "ENST00000000003"; gene_name "TESTLNC"; transcript_biotype "lncRNA";',
        '7\thavana\texon\t5000\t5500\t.\t+\t.\tgene_id "ENSG00000000003"; transcript_id "ENST00000000003";',
    ]
)

# Ensembl's CDS FASTA sequence is stop-inclusive (confirmed live,
# BRCA1's ENST00000357654: 5592nt = 1864 codons = 1863 aa + stop) -- so
# this fixture's sequence spans the full stop-inclusive CDS region
# (1000-1206, 207nt), not just the GTF `CDS` feature's own 1003-1206
# (204nt, which excludes the separate `stop_codon` feature at
# 1000-1002). 207nt = 69 codons = 68 aa + 1 stop.
_CDS_SEQUENCE = "ATG" + "AAA" * 67 + "TAA"  # 3 + 201 + 3 = 207nt
assert len(_CDS_SEQUENCE) == 207
_CDS_FASTA_BODY = f">ENST00000000002.1 cds chromosome:GRCh38:7:1000:2000:-1 gene:ENSG00000000001.1 gene_biotype:protein_coding transcript_biotype:protein_coding gene_symbol:TESTG1 description:test gene\n{_CDS_SEQUENCE}\n"


def _fake_config(**overrides):
    cfg = mock.Mock()
    cfg.ensembl.AUTO_FETCH_ENABLED = True
    cfg.ensembl.OFFLINE_MODE = False
    cfg.ensembl.GTF_INDEX_URL = _INDEX_URL
    cfg.ensembl.CDS_FASTA_URL = "https://x.invalid/cds.fa.gz"
    cfg.ensembl.AUTO_FETCH_TTL_HOURS = 168.0
    cfg.ensembl.AUTO_FETCH_TIMEOUT_SECS = 60
    for key, value in overrides.items():
        setattr(cfg.ensembl, key, value)
    return cfg


def _index_response():
    return mock.Mock(status_code=200, text=_GTF_INDEX_HTML_EXCERPT, raise_for_status=lambda: None)


def _gzip_stream_response(text: str):
    resp = mock.Mock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.raw = io.BytesIO(gzip.compress(text.encode("utf-8")))
    return resp


class TestDiscoverCurrentGtfUrl(unittest.TestCase):
    def test_parses_real_directory_listing_shape_and_picks_the_unqualified_file(self):
        with mock.patch("pipeline.ensembl.bootstrap.requests.get", return_value=_index_response()):
            url, version = ensembl_bootstrap._discover_current_gtf_url(_INDEX_URL)
        self.assertEqual(url, _INDEX_URL + "Homo_sapiens.GRCh38.116.gtf.gz")
        self.assertEqual(version, "Ensembl release 116")

    def test_does_not_match_abinitio_or_chr_variants(self):
        with mock.patch("pipeline.ensembl.bootstrap.requests.get", return_value=_index_response()):
            url, _ = ensembl_bootstrap._discover_current_gtf_url(_INDEX_URL)
        self.assertNotIn("abinitio", url)
        self.assertNotIn(".chr.", url)
        self.assertNotIn("chr_patch_hapl_scaff", url)

    def test_request_failure_returns_none_none(self):
        with mock.patch("pipeline.ensembl.bootstrap.requests.get", side_effect=requests.ConnectionError("refused")):
            url, version = ensembl_bootstrap._discover_current_gtf_url(_INDEX_URL)
        self.assertIsNone(url)
        self.assertIsNone(version)

    def test_unrecognized_html_shape_returns_none_none(self):
        response = mock.Mock(status_code=200, text="<html>nothing useful here</html>", raise_for_status=lambda: None)
        with mock.patch("pipeline.ensembl.bootstrap.requests.get", return_value=response):
            url, version = ensembl_bootstrap._discover_current_gtf_url(_INDEX_URL)
        self.assertIsNone(url)
        self.assertIsNone(version)


class TestParseGtfLines(unittest.TestCase):
    def test_chooses_the_ensembl_canonical_transcript_over_a_plain_one(self):
        genes = ensembl_bootstrap._parse_gtf_lines(_GTF_BODY.splitlines())
        self.assertIn("TESTG1", genes)
        self.assertEqual(genes["TESTG1"]["transcript"]["transcript_id"], "ENST00000000002")
        self.assertTrue(genes["TESTG1"]["transcript"]["is_canonical"])
        self.assertTrue(genes["TESTG1"]["transcript"]["is_mane_select"])

    def test_non_protein_coding_gene_is_excluded(self):
        genes = ensembl_bootstrap._parse_gtf_lines(_GTF_BODY.splitlines())
        self.assertNotIn("TESTLNC", genes)

    def test_gene_span_and_exons_captured(self):
        genes = ensembl_bootstrap._parse_gtf_lines(_GTF_BODY.splitlines())
        entry = genes["TESTG1"]
        self.assertEqual(entry["chrom"], "7")
        self.assertEqual(entry["gene_start"], 1000)
        self.assertEqual(entry["gene_end"], 2000)
        self.assertEqual(entry["strand"], -1)
        exon_spans = {(e["start"], e["end"]) for e in entry["transcript"]["exons"]}
        self.assertEqual(exon_spans, {(1900, 2000), (1000, 1206)})

    def test_cds_bounds_are_widened_to_include_the_stop_codon(self):
        # CDS feature alone spans 1003-1206; the stop_codon feature
        # (1000-1002) must be unioned in, matching Ensembl REST's own
        # stop-inclusive Translation.start/end convention.
        genes = ensembl_bootstrap._parse_gtf_lines(_GTF_BODY.splitlines())
        transcript = genes["TESTG1"]["transcript"]
        self.assertEqual(transcript["cds_genomic_start"], 1000)
        self.assertEqual(transcript["cds_genomic_end"], 1206)

    def test_protein_length_excludes_the_stop_codon(self):
        genes = ensembl_bootstrap._parse_gtf_lines(_GTF_BODY.splitlines())
        # (1206-1000+1)=207nt stop-inclusive CDS -> 69 codons -> 68 aa.
        self.assertEqual(genes["TESTG1"]["transcript"]["protein_length"], 68)

    def test_malformed_lines_are_skipped_not_fatal(self):
        lines = _GTF_BODY.splitlines() + ["not\ta\tvalid\tgtf\tline", "", "# a comment"]
        genes = ensembl_bootstrap._parse_gtf_lines(lines)
        self.assertIn("TESTG1", genes)

    def test_gene_with_no_coding_transcript_still_gets_a_span_only_entry(self):
        """A protein_coding-biotype gene whose only transcript doesn't
        itself qualify (no exons/CDS) must still appear -- with
        `transcript: None` -- so `genes_overlapping` doesn't silently
        lose its gene-body span (see `_choose_and_serialize`'s docstring)."""
        lines = [
            '1\thavana\tgene\t10\t20\t.\t+\t.\tgene_id "ENSG00000000009"; gene_name "NOCODING"; gene_biotype "protein_coding";',
            '1\thavana\ttranscript\t10\t20\t.\t+\t.\tgene_id "ENSG00000000009"; transcript_id "ENST00000000009"; transcript_biotype "retained_intron";',
            '1\thavana\texon\t10\t20\t.\t+\t.\tgene_id "ENSG00000000009"; transcript_id "ENST00000000009";',
        ]
        genes = ensembl_bootstrap._parse_gtf_lines(lines)
        self.assertIn("NOCODING", genes)
        self.assertIsNone(genes["NOCODING"]["transcript"])
        self.assertEqual(genes["NOCODING"]["gene_start"], 10)
        self.assertEqual(genes["NOCODING"]["gene_end"], 20)


class TestParseCdsFastaLines(unittest.TestCase):
    def test_extracts_sequence_for_a_wanted_transcript_and_strips_version(self):
        sequences = ensembl_bootstrap._parse_cds_fasta_lines(_CDS_FASTA_BODY.splitlines(), {"ENST00000000002"})
        self.assertEqual(sequences["ENST00000000002"], _CDS_SEQUENCE)

    def test_transcripts_not_in_the_wanted_set_are_skipped(self):
        sequences = ensembl_bootstrap._parse_cds_fasta_lines(_CDS_FASTA_BODY.splitlines(), {"ENST99999999999"})
        self.assertEqual(sequences, {})

    def test_multi_record_file_only_captures_wanted_records(self):
        body = (
            _CDS_FASTA_BODY
            + ">ENST00000099999.3 cds chromosome:GRCh38:1:1:9:1 gene:ENSG9 gene_symbol:OTHER\nACGACGACG\n"
        )
        sequences = ensembl_bootstrap._parse_cds_fasta_lines(body.splitlines(), {"ENST00000000002"})
        self.assertEqual(set(sequences.keys()), {"ENST00000000002"})


class TestBuildDataset(unittest.TestCase):
    def test_full_build_merges_cds_sequence_into_transcript_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with mock.patch(
                "pipeline.ensembl.bootstrap.requests.get",
                side_effect=[_gzip_stream_response(_GTF_BODY), _gzip_stream_response(_CDS_FASTA_BODY)],
            ):
                ok = ensembl_bootstrap._build_dataset(
                    "https://x.invalid/gtf.gz", "https://x.invalid/cds.fa.gz", dest, "Ensembl release 116"
                )
            self.assertTrue(ok)
            with open(dest, "r", encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["gene_symbol"], "TESTG1")
            self.assertEqual(lines[0]["transcript"]["cds_sequence"], _CDS_SEQUENCE)

    def test_gtf_failure_aborts_the_whole_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with mock.patch("pipeline.ensembl.bootstrap.requests.get", side_effect=requests.ConnectionError("down")):
                ok = ensembl_bootstrap._build_dataset(
                    "https://x.invalid/gtf.gz", "https://x.invalid/cds.fa.gz", dest, "Ensembl release 116"
                )
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest))

    def test_cds_fasta_failure_still_caches_structure_without_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with mock.patch(
                "pipeline.ensembl.bootstrap.requests.get",
                side_effect=[_gzip_stream_response(_GTF_BODY), requests.ConnectionError("cds down")],
            ):
                ok = ensembl_bootstrap._build_dataset(
                    "https://x.invalid/gtf.gz", "https://x.invalid/cds.fa.gz", dest, "Ensembl release 116"
                )
            self.assertTrue(ok)
            with open(dest, "r", encoding="utf-8") as fh:
                record = json.loads(fh.readline())
            self.assertIsNone(record["transcript"]["cds_sequence"])

    def test_writes_provenance_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with mock.patch(
                "pipeline.ensembl.bootstrap.requests.get",
                side_effect=[_gzip_stream_response(_GTF_BODY), _gzip_stream_response(_CDS_FASTA_BODY)],
            ):
                ensembl_bootstrap._build_dataset(
                    "https://x.invalid/gtf.gz", "https://x.invalid/cds.fa.gz", dest, "Ensembl release 116"
                )
            sidecar_path = dest + ".provenance.json"
            self.assertTrue(os.path.exists(sidecar_path))
            with open(sidecar_path, "r", encoding="utf-8") as fh:
                sidecar = json.load(fh)
            self.assertEqual(sidecar["version"], "Ensembl release 116")


class TestEnsureDatasetFile(unittest.TestCase):
    def test_disabled_auto_fetch_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(AUTO_FETCH_ENABLED=False)),
            mock.patch("pipeline.ensembl.bootstrap.requests.get") as get,
        ):
            result = ensembl_bootstrap.ensure_dataset_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_offline_mode_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(OFFLINE_MODE=True)),
            mock.patch("pipeline.ensembl.bootstrap.requests.get") as get,
        ):
            result = ensembl_bootstrap.ensure_dataset_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_fresh_cache_is_reused_without_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write('{"gene_symbol": "TESTG1"}\n')
            with (
                mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.ensembl.bootstrap.requests.get") as get,
            ):
                result = ensembl_bootstrap.ensure_dataset_file()
            self.assertEqual(result, dest)
            get.assert_not_called()

    def test_full_successful_fetch_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch(
                    "pipeline.ensembl.bootstrap.requests.get",
                    side_effect=[
                        _index_response(),
                        _gzip_stream_response(_GTF_BODY),
                        _gzip_stream_response(_CDS_FASTA_BODY),
                    ],
                ),
            ):
                result = ensembl_bootstrap.ensure_dataset_file()
            self.assertEqual(result, os.path.join(tmp, "ensembl_transcripts.jsonl"))

    def test_index_fetch_failure_falls_back_to_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ensembl_transcripts.jsonl")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write('{"gene_symbol": "TESTG1"}\n')
            os.utime(dest, (0, 0))
            with (
                mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.ensembl.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = ensembl_bootstrap.ensure_dataset_file()
            self.assertEqual(result, dest)

    def test_index_fetch_failure_with_no_existing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.ensembl.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.ensembl.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = ensembl_bootstrap.ensure_dataset_file()
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
