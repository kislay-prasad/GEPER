"""Tests for pipeline/alphafold/{models,utils,cache,lookup}.py."""

import unittest
from unittest import mock

from pipeline.alphafold.cache import AlphaFoldCache
from pipeline.alphafold.lookup import AlphaFoldLookup
from pipeline.alphafold.models import AlphaFoldAnnotation, confidence_band
from pipeline.alphafold.utils import accession_cache_key, normalize_accession, parse_pdb_plddt

_SAMPLE_PDB = (
    "ATOM      1  N   MET A   1      11.104  13.207   2.100  1.00 95.63           N\n"
    "ATOM      2  CA  MET A   1      12.560  13.207   2.100  1.00 95.63           C\n"
    "ATOM      6  CA  ALA A   2      13.560  14.207   3.100  1.00 62.10           C\n"
    "ATOM     10  CA  GLY A   3      14.560  15.207   4.100  1.00 35.40           C\n"
)


class TestConfidenceBand(unittest.TestCase):
    def test_bands_match_alphafold_published_thresholds(self):
        kwargs = dict(very_high=90, confident=70, low=50)
        self.assertEqual(confidence_band(95, **kwargs), "very_high")
        self.assertEqual(confidence_band(80, **kwargs), "confident")
        self.assertEqual(confidence_band(60, **kwargs), "low")
        self.assertEqual(confidence_band(30, **kwargs), "very_low")
        self.assertIsNone(confidence_band(None, **kwargs))


class TestNormalizeAccession(unittest.TestCase):
    def test_normalize_and_cache_key(self):
        self.assertEqual(normalize_accession(" p04637 "), "P04637")
        self.assertEqual(accession_cache_key("p04637"), accession_cache_key("P04637"))


class TestParsePdbPlddt(unittest.TestCase):
    def test_extracts_one_value_per_ca_atom(self):
        residues = parse_pdb_plddt(_SAMPLE_PDB)
        self.assertEqual(residues, {1: 95.63, 2: 62.10, 3: 35.40})

    def test_malformed_line_is_skipped_not_raised(self):
        garbled = "ATOM this is not a valid pdb line at all\n" + _SAMPLE_PDB
        residues = parse_pdb_plddt(garbled)
        self.assertEqual(residues, {1: 95.63, 2: 62.10, 3: 35.40})

    def test_empty_text_returns_empty_dict(self):
        self.assertEqual(parse_pdb_plddt(""), {})


class TestAlphaFoldAnnotation(unittest.TestCase):
    def test_not_found_and_from_error_factories(self):
        nf = AlphaFoldAnnotation.not_found("P04637", "alphafold_db_api")
        self.assertFalse(nf.found)
        err = AlphaFoldAnnotation.from_error("P04637", "boom")
        self.assertEqual(err.error, "boom")

    def test_to_dict_shape(self):
        ann = AlphaFoldAnnotation(
            accession="P04637", source="alphafold_db_api", found=True, mean_plddt=88.2, mean_plddt_band="confident"
        )
        d = ann.to_dict()
        self.assertEqual(d["mean_plddt"], 88.2)
        self.assertEqual(d["mean_plddt_band"], "confident")


class TestAlphaFoldCache(unittest.TestCase):
    def test_put_get_roundtrip(self):
        cache = AlphaFoldCache(max_size=10, ttl_seconds=None)
        cache.put("accession:P04637", {"found": True})
        self.assertEqual(cache.get("accession:P04637"), {"found": True})


class TestAlphaFoldLookupDisabled(unittest.TestCase):
    def test_disabled_flag_skips_entirely(self):
        provider = mock.Mock()
        lookup = AlphaFoldLookup(provider=provider, cache=None)
        with mock.patch("pipeline.alphafold.lookup.CONFIG") as fake_config:
            fake_config.alphafold.ENABLED = False
            result = lookup.query_accession("P04637")
        provider.query.assert_not_called()
        self.assertTrue(result["skipped"])


class TestAlphaFoldLookupQueryVariant(unittest.TestCase):
    def test_no_accession_returns_informative_not_found(self):
        provider = mock.Mock()
        lookup = AlphaFoldLookup(provider=provider, cache=None)
        with mock.patch("pipeline.alphafold.lookup.CONFIG") as fake_config:
            fake_config.alphafold.ENABLED = True
            result = lookup.query_variant(uniprot_result={"found": False}, protein_position=150)

        provider.query.assert_not_called()
        self.assertFalse(result["found"])
        self.assertIn("accession", result["reason"])

    def test_position_specific_result_bypasses_cache(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {
            "found": True,
            "error": None,
            "affected_residue_plddt": 92.0,
            "affected_residue_band": "very_high",
        }
        provider.query.return_value = annotation

        cache = mock.Mock()
        lookup = AlphaFoldLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.alphafold.lookup.CONFIG") as fake_config:
            fake_config.alphafold.ENABLED = True
            result = lookup.query_variant(uniprot_result={"accession": "P04637"}, protein_position=150)

        provider.query.assert_called_once_with("P04637", protein_position=150)
        cache.get.assert_not_called()  # position-specific path must not read the accession-level cache
        self.assertEqual(result["affected_residue_plddt"], 92.0)

    def test_accession_level_query_uses_cache(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": True, "error": None, "mean_plddt": 80.0}
        annotation.error = None
        provider.query.return_value = annotation

        cache = AlphaFoldCache(max_size=10, ttl_seconds=None)
        lookup = AlphaFoldLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.alphafold.lookup.CONFIG") as fake_config:
            fake_config.alphafold.ENABLED = True
            first = lookup.query_accession("P04637")
            second = lookup.query_accession("P04637")

        self.assertEqual(provider.query.call_count, 1)
        self.assertEqual(second["source"], "cache")
        self.assertTrue(first["found"])


if __name__ == "__main__":
    unittest.main()
