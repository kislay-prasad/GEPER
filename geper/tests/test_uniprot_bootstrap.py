"""
Tests for `pipeline/uniprot/bootstrap.py` -- the self-provisioning
fetch+parse+convert of UniProt's human reference proteome Swiss-Prot
flat file into the JSON-lines shape
`pipeline/uniprot/provider.py::LocalDatasetUniProtProvider` already
expects. `requests.get` is mocked throughout (both the `RELEASE.metalink`
version fetch and the gzip flat-file download) -- no live network calls
in this test suite.

The synthetic flat-file fixture below is hand-built (not sliced from a
live download, unlike `tests/fixtures/mane_summary_stk11_cbarp.tsv`,
since a single real Swiss-Prot record can run 100KB+ -- too heavy for a
lightweight fixture) but is real, valid Swiss-Prot flat-file syntax,
parses with the same `Bio.SwissProt.parse` this module uses in
production, and deliberately exercises: a fully-annotated reviewed
entry (function + disease comment + feature), a gene with both an
unreviewed and a reviewed record (dedup must prefer reviewed,
regardless of which comes first in the file), a gene with only an
unreviewed record (still indexed -- partial coverage beats none), and
a record with no GN (gene name) line at all (must be skipped, nothing
to index it by). Real BRCA1/TP53 ground-truth verification against a
small extracted fixture lives in `tests/test_uniprot_provider.py`
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
from Bio import SwissProt

from pipeline.uniprot import bootstrap as uniprot_bootstrap

_METALINK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="http://www.metalinker.org/" version="3.0">
  <publisher><name>UniProt Consortium</name></publisher>
  <version>2026_02</version>
</metalink>
"""

_DIR_URL = "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/reference_proteomes/Eukaryota/UP000005640/"


def _record(entry_name, accession, gene_line, reviewed=True, disease=True):
    status = "Reviewed" if reviewed else "Unreviewed"
    gn = f"GN   {gene_line}\n" if gene_line else ""
    disease_line = (
        "CC   -!- DISEASE: Test disease (TESTD) [MIM:100000]: A test disease description.\n" if disease else ""
    )
    return f"""ID   {entry_name}             {status};         50 AA.
AC   {accession};
DT   01-JAN-2000, integrated into UniProtKB/Swiss-Prot.
DT   01-JAN-2000, sequence version 1.
DT   01-JAN-2020, entry version 5.
DE   RecName: Full=Test protein for {entry_name} {{ECO:0000269|PubMed:12345678}};
{gn}OS   Homo sapiens (Human).
OC   Eukaryota; Metazoa; Chordata; Craniata; Vertebrata; Euteleostomi; Mammalia.
OX   NCBI_TaxID=9606;
RN   [1]
RP   FUNCTION.
RX   PubMed=12345678;
RA   Doe J.;
RT   "A test paper.";
RL   J. Test. 1:1-2(2000).
CC   -!- FUNCTION: Does a test function for {entry_name}.
{disease_line}PE   1: Evidence at protein level;
KW   Reference proteome.
FT   CHAIN           1..50
FT                   /note="Test protein for {entry_name}"
FT                   /id="PRO_0000000001"
SQ   SEQUENCE   50 AA;  5500 MW;  1234567890ABCDEF CRC64;
     MASDFASDFA SDFASDFASD FASDFASDFA SDFASDFASD FASDFASDFA
//
"""


# GENE2TEST appears twice: unreviewed first, reviewed second -- the
# reviewed one must win regardless of file order.
_FLAT_FILE_TEXT = (
    _record("BRCA1TEST_HUMAN", "Q00001", "Name=BRCA1TEST;")
    + _record("GENE2TEST_HUMAN_UNREV", "Q00002", "Name=GENE2TEST;", reviewed=False, disease=False)
    + _record("GENE2TEST_HUMAN_REV", "Q00003", "Name=GENE2TEST;", reviewed=True, disease=False)
    + _record("GENE3TEST_HUMAN", "Q00004", "Name=GENE3TEST;", reviewed=False, disease=False)
    + _record("NOGENETEST_HUMAN", "Q00005", gene_line=None, disease=False)
)


def _metalink_response():
    return mock.Mock(status_code=200, text=_METALINK_XML, raise_for_status=lambda: None)


def _dat_gz_response(text=_FLAT_FILE_TEXT):
    resp = mock.Mock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    body = gzip.compress(text.encode("utf-8"))
    resp.iter_content = lambda chunk_size=1: [body]
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: False
    return resp


def _fake_config(**overrides):
    cfg = mock.Mock()
    cfg.uniprot.AUTO_FETCH_ENABLED = True
    cfg.uniprot.OFFLINE_MODE = False
    cfg.uniprot.AUTO_FETCH_DIR_URL = _DIR_URL
    cfg.uniprot.AUTO_FETCH_TTL_HOURS = 168.0
    cfg.uniprot.AUTO_FETCH_TIMEOUT_SECS = 60
    for key, value in overrides.items():
        setattr(cfg.uniprot, key, value)
    return cfg


class TestDiscoverVersion(unittest.TestCase):
    def test_parses_real_metalink_shape(self):
        with (
            mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_metalink_response()),
            mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config()),
        ):
            version = uniprot_bootstrap._discover_version(_DIR_URL)
        self.assertEqual(version, "2026_02")

    def test_request_failure_returns_none(self):
        with (
            mock.patch("pipeline.uniprot.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config()),
        ):
            version = uniprot_bootstrap._discover_version(_DIR_URL)
        self.assertIsNone(version)

    def test_malformed_xml_returns_none(self):
        bad = mock.Mock(status_code=200, text="not xml at all <<<", raise_for_status=lambda: None)
        with (
            mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=bad),
            mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config()),
        ):
            version = uniprot_bootstrap._discover_version(_DIR_URL)
        self.assertIsNone(version)


class TestFieldExtraction(unittest.TestCase):
    def test_extract_protein_name_strips_evidence_tags(self):
        name = uniprot_bootstrap._extract_protein_name("RecName: Full=Test Protein {ECO:0000269|PubMed:12345678};")
        self.assertEqual(name, "Test Protein")

    def test_extract_protein_name_falls_back_to_submitted_name(self):
        name = uniprot_bootstrap._extract_protein_name("SubName: Full=Predicted Protein;")
        self.assertEqual(name, "Predicted Protein")

    def test_extract_organism_strips_common_name_and_period(self):
        name = uniprot_bootstrap._extract_organism_scientific_name("Homo sapiens (Human).")
        self.assertEqual(name, "Homo sapiens")

    def test_entry_type_literals_match_live_rest_api(self):
        # Confirmed live 2026-08-08 against a real REST response --
        # `pipeline/uniprot/utils.py::_entry_type_is_reviewed` substring
        # matches against these exact strings.
        self.assertEqual(uniprot_bootstrap._entry_type("Reviewed"), "UniProtKB reviewed (Swiss-Prot)")
        self.assertEqual(uniprot_bootstrap._entry_type("Unreviewed"), "UniProtKB unreviewed (TrEMBL)")

    def test_comments_to_json_extracts_function_and_disease_only(self):
        comments = [
            "FUNCTION: Does a thing.",
            "SUBCELLULAR LOCATION: Nucleus.",
            "DISEASE: Some disease (SD) [MIM:100000]: A description here.",
        ]
        result = uniprot_bootstrap._comments_to_json(comments)
        types = [c["commentType"] for c in result]
        self.assertEqual(types, ["FUNCTION", "DISEASE"])
        self.assertEqual(result[0]["texts"][0]["value"], "Does a thing.")
        self.assertEqual(result[1]["disease"]["diseaseId"], "Some disease (SD) [MIM:100000]")
        self.assertEqual(result[1]["disease"]["description"], "A description here.")

    def test_comments_to_json_only_keeps_first_function(self):
        comments = ["FUNCTION: First.", "FUNCTION: Second (should be dropped)."]
        result = uniprot_bootstrap._comments_to_json(comments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["texts"][0]["value"], "First.")

    def test_comments_to_json_handles_free_text_disease_note(self):
        # Real shape confirmed live (TP53): "DISEASE: Note=<free text
        # with no 'Name [MIM:x]: description' structure>".
        comments = ["DISEASE: Note=Some free-text disease relevance note with no colon-delimited structure at all."]
        result = uniprot_bootstrap._comments_to_json(comments)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["disease"]["diseaseId"].startswith("Note="))
        self.assertEqual(result[0]["disease"]["description"], "")


class TestPositionValue(unittest.TestCase):
    """
    Regression coverage for the real production bug (2026-08-08): a
    live UniProt run crashed on every single variant with
    `TypeError: int() argument must be a string, a bytes-like object
    or a real number, not 'UnknownPosition'` -- `Bio.SeqFeature.
    UnknownPosition` is Swiss-Prot's own `?` marker for a feature
    boundary that is genuinely unclear in the source record, and is
    NOT an `int` subclass the way every other `Position` type is, so
    a bare `int(location.start)` raised on it. `_position_value` is
    the fix: it must return `None` for `UnknownPosition` (never guess,
    never raise) and a normal int for everything else.
    """

    def test_unknown_position_returns_none_not_a_raise(self):
        from Bio.SeqFeature import UnknownPosition

        self.assertIsNone(uniprot_bootstrap._position_value(UnknownPosition()))

    def test_exact_position_converts_normally(self):
        from Bio.SeqFeature import ExactPosition

        self.assertEqual(uniprot_bootstrap._position_value(ExactPosition(42)), 42)

    def test_before_and_after_position_convert_normally(self):
        # Fuzzy but still-int-subclass Position types (Swiss-Prot's
        # `<`/`>` boundary markers) must keep working exactly as before
        # -- only UnknownPosition's bare `?` is the special case.
        from Bio.SeqFeature import AfterPosition, BeforePosition

        self.assertEqual(uniprot_bootstrap._position_value(BeforePosition(10)), 10)
        self.assertEqual(uniprot_bootstrap._position_value(AfterPosition(20)), 20)

    def test_plain_int_converts_normally(self):
        self.assertEqual(uniprot_bootstrap._position_value(7), 7)


# A real, valid Swiss-Prot flat-file record whose FT lines use the
# literal `?` position Swiss-Prot publishes for a genuinely unclear
# feature boundary (e.g. an uncertain signal-peptide cleavage site) --
# confirmed via `Bio.SeqFeature.Position.fromstring`: `text == "?"` ->
# `UnknownPosition()`. This is the exact shape that crashed the real
# 2026-08-08 production run; a fully-resolved DOMAIN feature is
# included alongside to confirm normal features in the same record are
# untouched by the fix.
_UNKNOWN_POSITION_RECORD_TEXT = """ID   UNKNOWNPOS_HUMAN         Reviewed;        100 AA.
AC   Q00099;
DT   01-JAN-2000, integrated into UniProtKB/Swiss-Prot.
DT   01-JAN-2000, sequence version 1.
DT   01-JAN-2020, entry version 5.
DE   RecName: Full=Test protein with an unclear signal peptide {ECO:0000269|PubMed:12345678};
GN   Name=UNKPOSTEST;
OS   Homo sapiens (Human).
OC   Eukaryota; Metazoa; Chordata; Craniata; Vertebrata; Euteleostomi; Mammalia.
OX   NCBI_TaxID=9606;
RN   [1]
RP   FUNCTION.
RX   PubMed=12345678;
RA   Doe J.;
RT   "A test paper.";
RL   J. Test. 1:1-2(2000).
CC   -!- FUNCTION: Does a test function for UNKNOWNPOS.
PE   1: Evidence at protein level;
KW   Reference proteome.
FT   SIGNAL          1..?
FT   CHAIN           ?..100
FT                   /note="Test chain with unknown start"
FT   DOMAIN          10..50
FT                   /note="A normal, fully-resolved domain"
SQ   SEQUENCE   100 AA;  11000 MW;  1234567890ABCDEF CRC64;
     MASDFASDFA SDFASDFASD FASDFASDFA SDFASDFASD FASDFASDFA MASDFASDFA
     SDFASDFASD FASDFASDFA SDFASDFASD FASDFASDFA
//
"""


class TestFeaturesToJsonUnknownPositionRegression(unittest.TestCase):
    """Exercises the real `Bio.SwissProt.parse` -> `_features_to_json` path directly (no network/bootstrap involved)."""

    def _record(self):
        return next(SwissProt.parse(io.StringIO(_UNKNOWN_POSITION_RECORD_TEXT)))

    def test_does_not_raise(self):
        # This is the literal crash from the real run -- calling
        # `_record_to_entry_json` on a record with an UnknownPosition
        # feature used to raise TypeError here.
        uniprot_bootstrap._record_to_entry_json(self._record())  # must not raise

    def test_unknown_boundary_features_are_kept_with_null_position(self):
        entry = uniprot_bootstrap._record_to_entry_json(self._record())
        by_type = {f["type"]: f for f in entry["features"]}

        self.assertIn("SIGNAL", by_type)
        self.assertEqual(by_type["SIGNAL"]["location"]["start"]["value"], 1)
        self.assertIsNone(by_type["SIGNAL"]["location"]["end"]["value"])

        self.assertIn("CHAIN", by_type)
        self.assertIsNone(by_type["CHAIN"]["location"]["start"]["value"])
        self.assertEqual(by_type["CHAIN"]["location"]["end"]["value"], 100)

    def test_normal_feature_in_the_same_record_is_unaffected(self):
        entry = uniprot_bootstrap._record_to_entry_json(self._record())
        by_type = {f["type"]: f for f in entry["features"]}
        self.assertEqual(by_type["DOMAIN"]["location"]["start"]["value"], 10)
        self.assertEqual(by_type["DOMAIN"]["location"]["end"]["value"], 50)

    def test_rest_of_the_record_still_parses(self):
        # One feature's unknown boundary must not degrade anything else
        # about this protein's cached data.
        entry = uniprot_bootstrap._record_to_entry_json(self._record())
        self.assertEqual(entry["primaryAccession"], "Q00099")
        self.assertEqual(
            entry["proteinDescription"]["recommendedName"]["fullName"]["value"],
            "Test protein with an unclear signal peptide",
        )
        self.assertEqual(entry["sequence"]["length"], 100)
        self.assertEqual(entry["comments"][0]["texts"][0]["value"], "Does a test function for UNKNOWNPOS.")


class TestDownloadAndConvert(unittest.TestCase):
    def test_parses_and_converts_all_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                ok = uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            self.assertTrue(ok)
            with open(dest, "r", encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
        genes = {row["gene_symbol"] for row in lines}
        self.assertEqual(genes, {"BRCA1TEST", "GENE2TEST", "GENE3TEST"})

    def test_gene_with_no_gn_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            with open(dest, "r", encoding="utf-8") as fh:
                accessions = [json.loads(line)["entry"]["primaryAccession"] for line in fh if line.strip()]
        self.assertNotIn("Q00005", accessions)  # NOGENETEST's accession

    def test_reviewed_entry_wins_regardless_of_file_order(self):
        """GENE2TEST appears unreviewed-then-reviewed in the fixture --
        the reviewed record (Q00003) must be the one indexed, not
        overwritten back to unreviewed by file order."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            with open(dest, "r", encoding="utf-8") as fh:
                rows = {json.loads(line)["gene_symbol"]: json.loads(line)["entry"] for line in fh if line.strip()}
        self.assertEqual(rows["GENE2TEST"]["primaryAccession"], "Q00003")
        self.assertEqual(rows["GENE2TEST"]["entryType"], "UniProtKB reviewed (Swiss-Prot)")

    def test_unreviewed_only_gene_is_still_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            with open(dest, "r", encoding="utf-8") as fh:
                rows = {json.loads(line)["gene_symbol"]: json.loads(line)["entry"] for line in fh if line.strip()}
        self.assertEqual(rows["GENE3TEST"]["entryType"], "UniProtKB unreviewed (TrEMBL)")

    def test_writes_provenance_sidecar_with_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            with open(dest + ".provenance.json", "r", encoding="utf-8") as fh:
                sidecar = json.load(fh)
        self.assertEqual(sidecar["version"], "UniProt 2026_02")
        self.assertIsNotNone(sidecar["content_hash"])

    def test_raw_download_is_cleaned_up_after_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response()):
                uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            self.assertFalse(os.path.exists(dest + ".raw.dat.gz"))

    def test_request_failure_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", side_effect=requests.Timeout()):
                ok = uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest))

    def test_a_record_with_an_unknown_position_does_not_abort_the_whole_proteome_parse(self):
        """
        End-to-end regression for the real 2026-08-08 production bug:
        one record anywhere in the ~200k-record reference proteome with
        an UnknownPosition feature used to raise out of the
        `SwissProt.parse` loop entirely, so `_download_and_convert`
        never reached its write step -- the cache file was never
        created, `ensure_dataset_file()` never returned a path, and
        every other, perfectly-good record in the same file (BRCA1TEST,
        GENE2TEST, GENE3TEST) was silently lost too. Mixes the
        UnknownPosition record into the same flat file as the existing
        fixture's good records to prove the fix isolates the damage to
        that one record.
        """
        mixed_text = _FLAT_FILE_TEXT + _UNKNOWN_POSITION_RECORD_TEXT
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with mock.patch("pipeline.uniprot.bootstrap.requests.get", return_value=_dat_gz_response(mixed_text)):
                ok = uniprot_bootstrap._download_and_convert("https://x.invalid/f.gz", dest, "2026_02")
            self.assertTrue(ok)
            with open(dest, "r", encoding="utf-8") as fh:
                rows = {json.loads(line)["gene_symbol"]: json.loads(line)["entry"] for line in fh if line.strip()}

        # The good records survive alongside the UnknownPosition one.
        self.assertEqual(set(rows.keys()), {"BRCA1TEST", "GENE2TEST", "GENE3TEST", "UNKPOSTEST"})
        unkpos_features = {f["type"]: f for f in rows["UNKPOSTEST"]["features"]}
        self.assertIsNone(unkpos_features["CHAIN"]["location"]["start"]["value"])


class TestEnsureDatasetFile(unittest.TestCase):
    def test_disabled_auto_fetch_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(AUTO_FETCH_ENABLED=False)),
            mock.patch("pipeline.uniprot.bootstrap.requests.get") as get,
        ):
            result = uniprot_bootstrap.ensure_dataset_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_offline_mode_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(OFFLINE_MODE=True)),
            mock.patch("pipeline.uniprot.bootstrap.requests.get") as get,
        ):
            result = uniprot_bootstrap.ensure_dataset_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_fresh_cache_is_reused_without_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write('{"gene_symbol": "X", "entry": {}}\n')
            with (
                mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(AUTO_FETCH_LOCAL_DIR=tmp)),
                mock.patch("pipeline.uniprot.bootstrap.requests.get") as get,
            ):
                result = uniprot_bootstrap.ensure_dataset_file()
            self.assertEqual(result, dest)
            get.assert_not_called()

    def test_full_successful_fetch_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(AUTO_FETCH_LOCAL_DIR=tmp)),
                mock.patch(
                    "pipeline.uniprot.bootstrap.requests.get",
                    side_effect=[_metalink_response(), _dat_gz_response()],
                ),
            ):
                result = uniprot_bootstrap.ensure_dataset_file()
            expected = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            self.assertEqual(result, expected)
            with open(expected, "r", encoding="utf-8") as fh:
                genes = {json.loads(line)["gene_symbol"] for line in fh if line.strip()}
            self.assertIn("BRCA1TEST", genes)

    def test_fetch_failure_falls_back_to_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "uniprot_local_dataset.jsonl")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write('{"gene_symbol": "X", "entry": {}}\n')
            os.utime(dest, (0, 0))  # force staleness
            with (
                mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(AUTO_FETCH_LOCAL_DIR=tmp)),
                mock.patch("pipeline.uniprot.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = uniprot_bootstrap.ensure_dataset_file()
            self.assertEqual(result, dest)

    def test_fetch_failure_with_no_existing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.uniprot.bootstrap.CONFIG", _fake_config(AUTO_FETCH_LOCAL_DIR=tmp)),
                mock.patch("pipeline.uniprot.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = uniprot_bootstrap.ensure_dataset_file()
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
