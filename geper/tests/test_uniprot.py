"""Tests for pipeline/uniprot/{models,utils,cache,lookup}.py."""

import unittest
from unittest import mock

from pipeline.clingen.utils import GeneResolution, GeneResolutionStatus
from pipeline.uniprot.cache import UniProtCache
from pipeline.uniprot.lookup import UniProtLookup
from pipeline.uniprot.models import UniProtAnnotation, UniProtFeature
from pipeline.uniprot.utils import gene_cache_key, normalize_gene_symbol, parse_uniprot_entry
from pipeline.vcf_parser import Variant


def _make_variant(chrom="17", pos=7676154, ref="G", alt="A") -> Variant:
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


_SAMPLE_ENTRY = {
    "primaryAccession": "P04637",
    "uniProtkbId": "P53_HUMAN",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
    "organism": {"scientificName": "Homo sapiens"},
    "sequence": {"length": 393},
    "comments": [
        {"commentType": "FUNCTION", "texts": [{"value": "Acts as a tumor suppressor."}]},
        {
            "commentType": "DISEASE",
            "disease": {"diseaseId": "Li-Fraumeni syndrome", "description": "A cancer predisposition syndrome."},
        },
    ],
    "features": [
        {"type": "Domain", "description": "DNA-binding", "location": {"start": {"value": 94}, "end": {"value": 289}}},
    ],
}


class TestUniProtUtilsParsing(unittest.TestCase):
    def test_normalize_gene_symbol(self):
        self.assertEqual(normalize_gene_symbol(" tp53 "), "TP53")
        self.assertIsNone(normalize_gene_symbol(""))
        self.assertIsNone(normalize_gene_symbol(None))

    def test_gene_cache_key_is_stable_across_case(self):
        self.assertEqual(gene_cache_key("tp53"), gene_cache_key("TP53"))

    def test_parse_uniprot_entry_extracts_all_fields(self):
        annotation = parse_uniprot_entry("TP53", _SAMPLE_ENTRY, source="uniprot_rest_api")
        self.assertTrue(annotation.found)
        self.assertEqual(annotation.accession, "P04637")
        self.assertEqual(annotation.entry_name, "P53_HUMAN")
        self.assertEqual(annotation.protein_name, "Cellular tumor antigen p53")
        self.assertTrue(annotation.reviewed)
        self.assertEqual(annotation.sequence_length, 393)
        self.assertIn("tumor suppressor", annotation.function_text)
        self.assertEqual(len(annotation.disease_comments), 1)
        self.assertIn("Li-Fraumeni syndrome", annotation.disease_comments[0])
        self.assertEqual(len(annotation.features), 1)
        self.assertEqual(annotation.features[0].begin, 94)
        self.assertEqual(annotation.features[0].end, 289)

    def test_unreviewed_entry_type_is_detected(self):
        entry = dict(_SAMPLE_ENTRY, entryType="UniProtKB unreviewed (TrEMBL)")
        annotation = parse_uniprot_entry("TP53", entry, source="uniprot_rest_api")
        self.assertFalse(annotation.reviewed)


class TestUniProtFeatureOverlap(unittest.TestCase):
    def test_overlaps_within_span(self):
        feat = UniProtFeature(feature_type="Domain", begin=10, end=20)
        self.assertTrue(feat.overlaps(15))
        self.assertTrue(feat.overlaps(10))
        self.assertTrue(feat.overlaps(20))

    def test_no_overlap_outside_span_or_missing_position(self):
        feat = UniProtFeature(feature_type="Domain", begin=10, end=20)
        self.assertFalse(feat.overlaps(9))
        self.assertFalse(feat.overlaps(21))
        self.assertFalse(feat.overlaps(None))


class TestUniProtAnnotationToDict(unittest.TestCase):
    def test_not_found_and_from_error_factories(self):
        nf = UniProtAnnotation.not_found("TP53", "uniprot_rest_api")
        self.assertFalse(nf.found)
        self.assertIsNone(nf.error)

        err = UniProtAnnotation.from_error("TP53", "boom")
        self.assertFalse(err.found)
        self.assertEqual(err.error, "boom")
        self.assertEqual(err.source, "error")

    def test_to_dict_roundtrip_shape(self):
        annotation = parse_uniprot_entry("TP53", _SAMPLE_ENTRY, source="uniprot_rest_api")
        d = annotation.to_dict()
        self.assertEqual(d["accession"], "P04637")
        self.assertEqual(d["features"][0]["feature_type"], "Domain")


class TestUniProtCache(unittest.TestCase):
    def test_put_get_roundtrip(self):
        cache = UniProtCache(max_size=10, ttl_seconds=None)
        cache.put("gene:TP53", {"found": True})
        self.assertEqual(cache.get("gene:TP53"), {"found": True})

    def test_missing_key_returns_none(self):
        cache = UniProtCache(max_size=10, ttl_seconds=None)
        self.assertIsNone(cache.get("gene:MISSING"))


class TestUniProtLookupDisabled(unittest.TestCase):
    def test_disabled_flag_skips_entirely_without_touching_provider(self):
        provider = mock.Mock()
        lookup = UniProtLookup(provider=provider, cache=None)
        with mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = False
            result = lookup.query_gene("TP53")
        provider.query.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["found"])


class TestUniProtLookupCaching(unittest.TestCase):
    def test_second_call_hits_cache_not_provider(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": True, "accession": "P04637"}
        annotation.error = None
        provider.query.return_value = annotation

        cache = UniProtCache(max_size=100, ttl_seconds=None)
        lookup = UniProtLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = True
            first = lookup.query_gene("TP53")
            second = lookup.query_gene("tp53")  # case-insensitive cache key

        self.assertEqual(provider.query.call_count, 1)
        self.assertEqual(second["source"], "cache")
        self.assertTrue(first["found"])

    def test_error_results_are_not_cached(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": False, "error": "timeout"}
        annotation.error = "timeout"
        provider.query.return_value = annotation

        cache = UniProtCache(max_size=100, ttl_seconds=None)
        lookup = UniProtLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = True
            lookup.query_gene("TP53")
            lookup.query_gene("TP53")

        self.assertEqual(provider.query.call_count, 2)


class TestUniProtLookupQueryVariant(unittest.TestCase):
    def test_gene_symbol_hint_skips_gene_resolution(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": True, "accession": "P04637"}
        annotation.error = None
        provider.query.return_value = annotation

        lookup = UniProtLookup(provider=provider, cache=None)
        with (
            mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config,
            mock.patch("pipeline.uniprot.lookup.resolve_gene_symbol_detail") as fake_resolve,
        ):
            fake_config.uniprot.ENABLED = True
            result = lookup.query_variant(_make_variant(), assembly="GRCh38", gene_symbol_hint="TP53")

        fake_resolve.assert_not_called()
        self.assertEqual(result["gene_symbol"], "TP53")

    def test_no_gene_resolved_returns_informative_not_found(self):
        provider = mock.Mock()
        lookup = UniProtLookup(provider=provider, cache=None)
        not_found = GeneResolution(
            GeneResolutionStatus.NOT_FOUND, None, "none", reason="no gene overlaps this position."
        )
        with (
            mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config,
            mock.patch("pipeline.uniprot.lookup.resolve_gene_symbol_detail", return_value=not_found),
        ):
            fake_config.uniprot.ENABLED = True
            result = lookup.query_variant(_make_variant())

        provider.query.assert_not_called()
        self.assertFalse(result["found"])
        self.assertIn("gene", result["reason"])

    def test_ambiguous_gene_resolution_returns_clear_reason_not_a_guess(self):
        """Regression test for the STK11/CBARP-shaped case: when the
        gene overlap is genuinely ambiguous, this must never silently
        pick one candidate -- gene_symbol stays None and the reason
        names the tied candidates."""
        provider = mock.Mock()
        lookup = UniProtLookup(provider=provider, cache=None)
        ambiguous = GeneResolution(
            GeneResolutionStatus.AMBIGUOUS,
            None,
            "none",
            reason="2 protein-coding genes genuinely overlap this position (STK11, CBARP) and could not be disambiguated.",
            candidates=["STK11", "CBARP"],
        )
        with (
            mock.patch("pipeline.uniprot.lookup.CONFIG") as fake_config,
            mock.patch("pipeline.uniprot.lookup.resolve_gene_symbol_detail", return_value=ambiguous),
        ):
            fake_config.uniprot.ENABLED = True
            result = lookup.query_variant(_make_variant())

        provider.query.assert_not_called()
        self.assertFalse(result["found"])
        self.assertIsNone(result["gene_symbol"])
        self.assertEqual(result["gene_resolution_status"], "ambiguous")
        self.assertIn("STK11", result["reason"])
        self.assertIn("CBARP", result["reason"])


if __name__ == "__main__":
    unittest.main()
