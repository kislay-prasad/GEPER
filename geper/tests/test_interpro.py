"""Tests for pipeline/interpro/{models,utils,cache,lookup}.py."""

import unittest
from unittest import mock

from pipeline.interpro.cache import InterProCache
from pipeline.interpro.lookup import InterProLookup
from pipeline.interpro.models import InterProAnnotation, InterProDomainMatch
from pipeline.interpro.utils import accession_cache_key, normalize_accession, parse_interpro_response

_SAMPLE_PAYLOAD = {
    "results": [
        {
            "metadata": {"accession": "IPR002117", "name": "p53 tumour suppressor family", "type": "family", "source_database": "interpro"},
            "proteins": [{"entry_protein_locations": [{"fragments": [{"start": 94, "end": 289}]}]}],
        },
        {
            "metadata": {"accession": "PF00870", "name": "P53", "type": "domain", "source_database": "pfam", "integrated": "IPR002117"},
            "proteins": [{"entry_protein_locations": [{"fragments": [{"start": 100, "end": 280}]}]}],
        },
    ]
}


class TestInterProUtilsParsing(unittest.TestCase):
    def test_normalize_accession(self):
        self.assertEqual(normalize_accession(" p04637 "), "P04637")
        self.assertIsNone(normalize_accession(""))

    def test_accession_cache_key_stable(self):
        self.assertEqual(accession_cache_key("p04637"), accession_cache_key("P04637"))

    def test_parse_interpro_response_extracts_both_interpro_and_member_db_entries(self):
        annotation = parse_interpro_response("P04637", _SAMPLE_PAYLOAD, source="interpro_rest_api")
        self.assertTrue(annotation.found)
        self.assertEqual(len(annotation.domains), 2)

        interpro_entry = next(d for d in annotation.domains if d.member_database == "interpro")
        self.assertEqual(interpro_entry.interpro_accession, "IPR002117")
        self.assertEqual(interpro_entry.start, 94)
        self.assertEqual(interpro_entry.end, 289)

        pfam_entry = next(d for d in annotation.domains if d.member_database == "pfam")
        self.assertEqual(pfam_entry.member_accession, "PF00870")
        self.assertEqual(pfam_entry.interpro_accession, "IPR002117")  # inherited from "integrated"
        self.assertEqual(pfam_entry.start, 100)

    def test_empty_results_is_not_found(self):
        annotation = parse_interpro_response("P99999", {"results": []}, source="interpro_rest_api")
        self.assertFalse(annotation.found)
        self.assertEqual(annotation.domains, [])


class TestInterProDomainMatchOverlap(unittest.TestCase):
    def test_overlaps(self):
        d = InterProDomainMatch(start=100, end=200)
        self.assertTrue(d.overlaps(150))
        self.assertFalse(d.overlaps(50))
        self.assertFalse(d.overlaps(None))


class TestInterProAnnotationAffectedDomains(unittest.TestCase):
    def test_affected_domains_filters_by_position(self):
        annotation = parse_interpro_response("P04637", _SAMPLE_PAYLOAD, source="interpro_rest_api")
        affected = annotation.affected_domains(150)
        self.assertEqual(len(affected), 2)  # both spans (94-289, 100-280) contain 150

        none_affected = annotation.affected_domains(500)
        self.assertEqual(none_affected, [])

        self.assertEqual(annotation.affected_domains(None), [])


class TestInterProCache(unittest.TestCase):
    def test_put_get_roundtrip(self):
        cache = InterProCache(max_size=10, ttl_seconds=None)
        cache.put("accession:P04637", {"found": True})
        self.assertEqual(cache.get("accession:P04637"), {"found": True})


class TestInterProLookupDisabled(unittest.TestCase):
    def test_disabled_flag_skips_entirely(self):
        provider = mock.Mock()
        lookup = InterProLookup(provider=provider, cache=None)
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = False
            result = lookup.query_accession("P04637")
        provider.query.assert_not_called()
        self.assertTrue(result["skipped"])


class TestInterProLookupQueryVariant(unittest.TestCase):
    def test_no_accession_returns_informative_not_found(self):
        provider = mock.Mock()
        lookup = InterProLookup(provider=provider, cache=None)
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = True
            result = lookup.query_variant(uniprot_result={"found": False}, protein_position=150)

        provider.query.assert_not_called()
        self.assertFalse(result["found"])
        self.assertIn("accession", result["reason"])

    def test_affected_domains_populated_when_position_given(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {
            "found": True,
            "error": None,
            "domains": [{"name": "p53 family", "start": 94, "end": 289}, {"name": "other", "start": 300, "end": 400}],
        }
        annotation.error = None
        provider.query.return_value = annotation

        cache = InterProCache(max_size=10, ttl_seconds=None)
        lookup = InterProLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = True
            result = lookup.query_variant(uniprot_result={"accession": "P04637"}, protein_position=150)

        self.assertEqual(len(result["affected_domains"]), 1)
        self.assertEqual(result["affected_domains"][0]["name"], "p53 family")
        self.assertEqual(result["protein_position_basis"], "transcript_cds")
        self.assertEqual(result["protein_position"], 150)

    def test_affected_domains_excludes_family_type_entries(self):
        # Regression test for the PM1-accuracy fix in
        # pipeline/interpro/lookup.py: a "family" entry (gene-family
        # membership, often spanning nearly the whole protein -- see
        # tests/test_pm1_interpro.py's real BRCA1 example) must not
        # count as a "domain overlap"; a real "domain" entry at the
        # same position must.
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {
            "found": True,
            "error": None,
            "domains": [
                {"name": "whole-gene family", "type": "family", "start": 1, "end": 1000},
                {"name": "real domain", "type": "domain", "start": 90, "end": 110},
            ],
        }
        annotation.error = None
        provider.query.return_value = annotation

        lookup = InterProLookup(provider=provider, cache=None)
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = True
            result = lookup.query_variant(uniprot_result={"accession": "P04637"}, protein_position=100)

        self.assertEqual(len(result["affected_domains"]), 1)
        self.assertEqual(result["affected_domains"][0]["name"], "real domain")

    def test_no_position_leaves_affected_domains_unknown_not_empty(self):
        # Regression test for the PM1 false-negative fix: a missing
        # protein_position (domain overlap never checked) must be
        # reported as None, not silently coerced to an empty list --
        # `[]` means "checked, no overlap", which is a different,
        # stronger claim than "position unknown". See
        # pipeline/interpro/lookup.py::query_variant's docstring.
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": True, "error": None, "domains": [{"name": "x", "start": 1, "end": 10}]}
        annotation.error = None
        provider.query.return_value = annotation

        lookup = InterProLookup(provider=provider, cache=None)
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = True
            result = lookup.query_variant(uniprot_result={"accession": "P04637"}, protein_position=None)

        self.assertIsNone(result["affected_domains"])
        self.assertIsNone(result["protein_position_basis"])
        self.assertIsNone(result["protein_position"])


if __name__ == "__main__":
    unittest.main()
