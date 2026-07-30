"""
Tests for the PS3/BS3 functional-evidence integration
(`pipeline/functional_evidence/`) and the ACMG/AMP PS3/BS3 rules
(`pipeline/acmg_rules.py::ACMGRuleEngine._ps3`/`_bs3`).

Ground truth, not synthetic guesses: `tests/fixtures/ps3_bs3_erepo_brca1.json`,
`ps3_bs3_erepo_cftr.json`, and `ps3_bs3_erepo_tp53.json` are frozen,
verbatim responses from a real, live
`GET https://erepo.clinicalgenome.org/evrepo/api/classifications?gene=<GENE>`
call (fetched 2026-07-30) -- confirmed during feasibility research to
contain a real ENIGMA BRCA1/BRCA2 VCEP curation of
`NM_007294.4:c.135-1G>T` / `NC_000017.11:g.43106534C>A` with evidence
code `PS3: Met`, classified Pathogenic for BRCA1-related cancer
predisposition. The CFTR fixture is a real *empty* response
(`{"variantInterpretations": []}`) -- a genuine coverage gap, not a
request-shape bug.

`ps3_bs3_mavedb_brca1_scoreset_metadata.json` is a frozen, verbatim
`GET https://api.mavedb.org/api/v1/score-sets/urn:mavedb:00000097-0-2`
response (the real BRCA1 saturation-genome-editing "Normalized Scores"
score set, Findlay et al. 2018, PMID 30209399) -- including its real
investigator-provided `scoreCalibrations` thresholds (Functional/
Intermediate/Non-functional at -0.748/-1.328). `ps3_bs3_mavedb_brca1_variants.csv`
is a real, verbatim slice (31 of 3893 rows: one exact row confirmed
live during feasibility research, `NM_007294.3:c.5565A>T` -> score
-0.0153221623501722 -> "normal", plus 10 more rows from each of the
three real calibration buckets) of that score set's real per-variant
data.
"""

import json
import os
import unittest
from unittest import mock

import requests

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.functional_evidence.erepo_provider import ErepoFunctionalEvidenceProvider
from pipeline.functional_evidence.lookup import FunctionalEvidenceLookup
from pipeline.functional_evidence.mavedb_provider import MaveDBFunctionalEvidenceProvider
from pipeline.functional_evidence.models import FunctionalEvidenceRecord
from pipeline.functional_evidence.utils import (
    bare_evidence_code,
    normalize_hgvs_c,
    parse_evidence_code_strength,
)
from utils.exceptions import ExternalAPIError

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_json_fixture(name: str):
    with open(os.path.join(_FIXTURE_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_text_fixture(name: str) -> str:
    with open(os.path.join(_FIXTURE_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _fake_response(json_payload=None, text_payload=None, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.raise_for_status = mock.Mock()
    if json_payload is not None:
        response.json.return_value = json_payload
    if text_payload is not None:
        response.text = text_payload
    return response


# ---------------------------------------------------------------------------
# Small pure-logic helpers
# ---------------------------------------------------------------------------

class TestUtils(unittest.TestCase):
    def test_bare_evidence_code_strips_strength_suffix(self):
        self.assertEqual(bare_evidence_code("PS3_Moderate"), "PS3")
        self.assertEqual(bare_evidence_code("BS3"), "BS3")

    def test_parse_evidence_code_strength_uses_suffix_when_present(self):
        self.assertEqual(parse_evidence_code_strength("PS3_Moderate", default="strong"), "moderate")
        self.assertEqual(parse_evidence_code_strength("PP4_Strong", default="supporting"), "strong")

    def test_parse_evidence_code_strength_falls_back_to_default(self):
        self.assertEqual(parse_evidence_code_strength("PS3", default="strong"), "strong")
        self.assertEqual(parse_evidence_code_strength("PS3_Unrecognized", default="strong"), "strong")

    def test_normalize_hgvs_c_strips_transcript_version(self):
        self.assertEqual(normalize_hgvs_c("NM_007294.4:c.181T>G"), "NM_007294:c.181T>G")
        self.assertEqual(normalize_hgvs_c("NM_007294.3:c.181T>G"), "NM_007294:c.181T>G")

    def test_normalize_hgvs_c_none_is_none(self):
        self.assertIsNone(normalize_hgvs_c(None))
        self.assertIsNone(normalize_hgvs_c(""))


# ---------------------------------------------------------------------------
# ErepoFunctionalEvidenceProvider -- real BRCA1/TP53/CFTR fixture data
# ---------------------------------------------------------------------------

class TestErepoProvider(unittest.TestCase):
    def test_real_brca1_ps3_met_variant_is_found(self):
        payload = _load_json_fixture("ps3_bs3_erepo_brca1.json")
        provider = ErepoFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.erepo_provider.requests.get", return_value=_fake_response(json_payload=payload)):
            index = provider.fetch_gene_index("BRCA1")

        hgvs_g = "NC_000017.11:g.43106534C>A"
        self.assertIn(hgvs_g, index)
        records = index[hgvs_g]
        self.assertTrue(any(r.call == "PS3" for r in records))
        ps3 = next(r for r in records if r.call == "PS3")
        self.assertEqual(ps3.source, "clingen_erepo")
        self.assertEqual(ps3.strength, "strong")
        self.assertEqual(ps3.expert_panel, "ENIGMA BRCA1 and BRCA2 VCEP")
        self.assertEqual(ps3.classification_outcome, "Pathogenic")

    def test_real_cftr_gene_has_no_curated_variants(self):
        payload = _load_json_fixture("ps3_bs3_erepo_cftr.json")
        self.assertEqual(payload.get("variantInterpretations"), [])
        provider = ErepoFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.erepo_provider.requests.get", return_value=_fake_response(json_payload=payload)):
            index = provider.fetch_gene_index("CFTR")
        self.assertEqual(index, {})

    def test_real_tp53_gene_has_both_ps3_and_bs3_met_records(self):
        payload = _load_json_fixture("ps3_bs3_erepo_tp53.json")
        provider = ErepoFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.erepo_provider.requests.get", return_value=_fake_response(json_payload=payload)):
            index = provider.fetch_gene_index("TP53")

        all_records = [r for records in index.values() for r in records]
        self.assertTrue(any(r.call == "PS3" for r in all_records))
        self.assertTrue(any(r.call == "BS3" for r in all_records))

    def test_404_response_is_treated_as_empty_not_an_error(self):
        provider = ErepoFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.erepo_provider.requests.get", return_value=_fake_response(status_code=404)):
            index = provider.fetch_gene_index("NOTAREALGENE")
        self.assertEqual(index, {})

    def test_repeated_request_failure_raises_external_api_error(self):
        provider = ErepoFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.erepo_provider.requests.get", side_effect=requests.exceptions.ConnectionError("boom")), \
             mock.patch("pipeline.functional_evidence.erepo_provider.time.sleep"):
            with self.assertRaises(ExternalAPIError):
                provider.fetch_gene_index("BRCA1")


# ---------------------------------------------------------------------------
# MaveDBFunctionalEvidenceProvider -- real BRCA1 SGE fixture data
# ---------------------------------------------------------------------------

class TestMaveDBProvider(unittest.TestCase):
    def _provider_with_fixtures(self):
        search_payload = {
            "scoreSets": [
                {
                    "urn": "urn:mavedb:00000097-0-2",
                    "numVariants": 3893,
                    "targetGenes": [{"name": "BRCA1", "mappedHgncName": "BRCA1"}],
                }
            ]
        }
        metadata = _load_json_fixture("ps3_bs3_mavedb_brca1_scoreset_metadata.json")
        csv_text = _load_text_fixture("ps3_bs3_mavedb_brca1_variants.csv")

        provider = MaveDBFunctionalEvidenceProvider()

        def fake_get(url, timeout=None):
            if url.endswith("/variants/data"):
                return _fake_response(text_payload=csv_text)
            return _fake_response(json_payload=metadata)

        return provider, search_payload, fake_get

    def test_real_normal_classified_variant_produces_bs3_record(self):
        provider, search_payload, fake_get = self._provider_with_fixtures()
        with mock.patch("pipeline.functional_evidence.mavedb_provider.requests.post", return_value=_fake_response(json_payload=search_payload)), \
             mock.patch("pipeline.functional_evidence.mavedb_provider.requests.get", side_effect=fake_get):
            index = provider.fetch_gene_index("BRCA1")

        key = normalize_hgvs_c("NM_007294.3:c.5565A>T")
        self.assertIn(key, index)
        record = index[key]
        self.assertEqual(record.call, "BS3")
        self.assertEqual(record.functional_classification, "normal")
        self.assertAlmostEqual(record.raw_score, -0.0153221623501722)
        self.assertFalse(record.research_use_only)
        self.assertEqual(record.score_set_urn, "urn:mavedb:00000097-0-2")
        self.assertIn("Findlay", record.publication)

    def test_abnormal_classified_variant_produces_ps3_record(self):
        provider, search_payload, fake_get = self._provider_with_fixtures()
        with mock.patch("pipeline.functional_evidence.mavedb_provider.requests.post", return_value=_fake_response(json_payload=search_payload)), \
             mock.patch("pipeline.functional_evidence.mavedb_provider.requests.get", side_effect=fake_get):
            index = provider.fetch_gene_index("BRCA1")

        abnormal_records = [r for r in index.values() if r.functional_classification == "abnormal"]
        self.assertTrue(abnormal_records)
        for record in abnormal_records:
            self.assertEqual(record.call, "PS3")
            self.assertLess(record.raw_score, -1.328)

    def test_intermediate_classified_variants_contribute_no_call(self):
        """Real 'Intermediate' bucketed rows exist in the fixture --
        confirm they're correctly excluded from the index (not_specified
        is not usable PS3/BS3 evidence), rather than silently defaulting
        to one direction."""
        provider, search_payload, fake_get = self._provider_with_fixtures()
        with mock.patch("pipeline.functional_evidence.mavedb_provider.requests.post", return_value=_fake_response(json_payload=search_payload)), \
             mock.patch("pipeline.functional_evidence.mavedb_provider.requests.get", side_effect=fake_get):
            index = provider.fetch_gene_index("BRCA1")

        for record in index.values():
            self.assertIn(record.functional_classification, ("normal", "abnormal"))

    def test_gene_not_targeted_by_any_score_set_yields_empty_index(self):
        provider = MaveDBFunctionalEvidenceProvider()
        with mock.patch(
            "pipeline.functional_evidence.mavedb_provider.requests.post",
            return_value=_fake_response(json_payload={"scoreSets": []}),
        ):
            index = provider.fetch_gene_index("CFTR")
        self.assertEqual(index, {})

    def test_search_failure_raises_external_api_error(self):
        provider = MaveDBFunctionalEvidenceProvider()
        with mock.patch("pipeline.functional_evidence.mavedb_provider.requests.post", side_effect=requests.exceptions.ConnectionError("boom")), \
             mock.patch("pipeline.functional_evidence.mavedb_provider.time.sleep"):
            with self.assertRaises(ExternalAPIError):
                provider.fetch_gene_index("BRCA1")

    def test_score_set_missing_calibration_is_skipped_gracefully(self):
        provider = MaveDBFunctionalEvidenceProvider()
        search_payload = {"scoreSets": [{"urn": "urn:mavedb:00099999-a-1", "numVariants": 10, "targetGenes": [{"name": "BRCA1"}]}]}
        with mock.patch("pipeline.functional_evidence.mavedb_provider.requests.post", return_value=_fake_response(json_payload=search_payload)), \
             mock.patch("pipeline.functional_evidence.mavedb_provider.requests.get", return_value=_fake_response(json_payload={"scoreCalibrations": []})):
            index = provider.fetch_gene_index("BRCA1")
        self.assertEqual(index, {})


# ---------------------------------------------------------------------------
# FunctionalEvidenceLookup -- composite primary/secondary fallback logic
# ---------------------------------------------------------------------------

class TestFunctionalEvidenceLookupComposite(unittest.TestCase):
    def _record(self, call: str, source: str = "clingen_erepo") -> FunctionalEvidenceRecord:
        return FunctionalEvidenceRecord(source=source, call=call, strength="strong", matched_hgvs="")

    def test_erepo_hit_short_circuits_mavedb(self):
        erepo = mock.Mock()
        erepo.is_available.return_value = True
        erepo.fetch_gene_index.return_value = {"NC_000017.11:g.1A>T": [self._record("PS3")]}
        mavedb = mock.Mock()
        mavedb.is_available.return_value = True

        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb, cache=None)
        result = lookup.query_variant("BRCA1", hgvs_g="NC_000017.11:g.1A>T", hgvs_c="NM_1.1:c.1A>T")

        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "clingen_erepo")
        mavedb.fetch_gene_index.assert_not_called()

    def test_erepo_miss_falls_back_to_mavedb(self):
        erepo = mock.Mock()
        erepo.is_available.return_value = True
        erepo.fetch_gene_index.return_value = {}
        mavedb = mock.Mock()
        mavedb.is_available.return_value = True
        mavedb.fetch_gene_index.return_value = {"NM_1:c.1A>T": self._record("BS3", source="mavedb")}

        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb, cache=None)
        result = lookup.query_variant("BRCA1", hgvs_g="NC_000017.11:g.1A>T", hgvs_c="NM_1.1:c.1A>T")

        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "mavedb")

    def test_neither_source_found_reports_not_found(self):
        erepo = mock.Mock()
        erepo.is_available.return_value = True
        erepo.fetch_gene_index.return_value = {}
        mavedb = mock.Mock()
        mavedb.is_available.return_value = True
        mavedb.fetch_gene_index.return_value = {}

        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb, cache=None)
        result = lookup.query_variant("CFTR", hgvs_g="NC_000007.14:g.1A>T", hgvs_c="NM_1.1:c.1A>T")

        self.assertFalse(result["found"])
        self.assertEqual(result["source"], "none")

    def test_no_gene_symbol_is_reported_without_touching_either_provider(self):
        erepo = mock.Mock()
        mavedb = mock.Mock()
        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb, cache=None)
        result = lookup.query_variant(None, hgvs_g="NC_000017.11:g.1A>T")
        self.assertFalse(result["found"])
        erepo.fetch_gene_index.assert_not_called()
        mavedb.fetch_gene_index.assert_not_called()

    def test_erepo_provider_failure_falls_through_to_mavedb(self):
        erepo = mock.Mock()
        erepo.is_available.return_value = True
        erepo.fetch_gene_index.side_effect = RuntimeError("network exploded")
        mavedb = mock.Mock()
        mavedb.is_available.return_value = True
        mavedb.fetch_gene_index.return_value = {"NM_1:c.1A>T": self._record("PS3", source="mavedb")}

        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb, cache=None)
        result = lookup.query_variant("BRCA1", hgvs_g="NC_000017.11:g.1A>T", hgvs_c="NM_1.1:c.1A>T")

        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "mavedb")

    def test_gene_level_index_is_cached_across_variants(self):
        """Two variants in the same gene must only trigger one
        fetch_gene_index call per source -- the whole point of
        gene-level (not variant-level) caching."""
        erepo = mock.Mock()
        erepo.is_available.return_value = True
        erepo.fetch_gene_index.return_value = {}
        mavedb = mock.Mock()
        mavedb.is_available.return_value = True
        mavedb.fetch_gene_index.return_value = {}

        lookup = FunctionalEvidenceLookup(erepo_provider=erepo, mavedb_provider=mavedb)
        lookup.query_variant("BRCA1", hgvs_g="g.1", hgvs_c="c.1")
        lookup.query_variant("BRCA1", hgvs_g="g.2", hgvs_c="c.2")

        self.assertEqual(erepo.fetch_gene_index.call_count, 1)
        self.assertEqual(mavedb.fetch_gene_index.call_count, 1)


# ---------------------------------------------------------------------------
# PS3/BS3 -- ACMGRuleEngine, through real fixture-derived evidence
# ---------------------------------------------------------------------------

class TestPS3BS3Rules(unittest.TestCase):
    def test_no_functional_evidence_is_not_evaluated(self):
        for method in (ACMGRuleEngine._ps3, ACMGRuleEngine._bs3):
            result = method({"skipped": False, "found": False})
            self.assertEqual(result.status, "not_evaluated")

    def test_none_result_is_not_evaluated(self):
        for code, method in (("PS3", ACMGRuleEngine._ps3), ("BS3", ACMGRuleEngine._bs3)):
            result = method(None)
            self.assertEqual(result.status, "not_evaluated")
            self.assertEqual(result.code, code)

    def test_real_erepo_ps3_met_record_triggers_ps3(self):
        functional_evidence_result = {
            "found": True, "source": "clingen_erepo", "gene_symbol": "BRCA1",
            "records": [{
                "source": "clingen_erepo", "call": "PS3", "strength": "strong",
                "matched_hgvs": "NC_000017.11:g.43106534C>A",
                "expert_panel": "ENIGMA BRCA1 and BRCA2 VCEP",
                "classification_outcome": "Pathogenic", "condition": "BRCA1-related cancer predisposition",
            }],
        }
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        self.assertEqual(ps3.status, "triggered")
        self.assertEqual(ps3.strength, "strong")
        self.assertEqual(ps3.confidence, "High")
        self.assertIn("ENIGMA BRCA1 and BRCA2 VCEP", ps3.rationale)

        bs3 = ACMGRuleEngine._bs3(functional_evidence_result)
        self.assertEqual(bs3.status, "not_evaluated")

    def test_erepo_evidence_code_with_strength_suffix_is_honored(self):
        functional_evidence_result = {
            "found": True, "source": "clingen_erepo", "gene_symbol": "TP53",
            "records": [{"source": "clingen_erepo", "call": "PS3", "strength": "moderate", "matched_hgvs": "x"}],
        }
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        self.assertEqual(ps3.strength, "moderate")

    def test_mavedb_abnormal_record_triggers_ps3_at_moderate_strength(self):
        functional_evidence_result = {
            "found": True, "source": "mavedb", "gene_symbol": "BRCA1",
            "records": [{
                "source": "mavedb", "call": "PS3", "strength": "moderate",
                "matched_hgvs": "NM_007294.3:c.1A>T", "raw_score": -2.5,
                "score_set_urn": "urn:mavedb:00000097-0-2",
                "functional_classification": "abnormal", "research_use_only": False,
                "publication": "Findlay et al. 2018",
            }],
        }
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        self.assertEqual(ps3.status, "triggered")
        self.assertEqual(ps3.strength, "moderate")
        self.assertEqual(ps3.confidence, "Moderate")
        self.assertIn("MaveDB", ps3.evidence_sources)

    def test_mavedb_normal_record_triggers_bs3(self):
        functional_evidence_result = {
            "found": True, "source": "mavedb", "gene_symbol": "BRCA1",
            "records": [{
                "source": "mavedb", "call": "BS3", "strength": "moderate",
                "matched_hgvs": "NM_007294.3:c.5565A>T", "raw_score": -0.0153,
                "score_set_urn": "urn:mavedb:00000097-0-2",
                "functional_classification": "normal", "research_use_only": False,
                "publication": "Findlay et al. 2018",
            }],
        }
        bs3 = ACMGRuleEngine._bs3(functional_evidence_result)
        self.assertEqual(bs3.status, "triggered")
        self.assertEqual(bs3.strength, "moderate")

    def test_mavedb_research_use_only_calibration_is_capped_at_supporting_confidence(self):
        functional_evidence_result = {
            "found": True, "source": "mavedb", "gene_symbol": "SOMEGENE",
            "records": [{
                "source": "mavedb", "call": "PS3", "strength": "supporting",
                "matched_hgvs": "NM_1.1:c.1A>T", "raw_score": -3.0,
                "score_set_urn": "urn:mavedb:00099999-a-1",
                "functional_classification": "abnormal", "research_use_only": True,
            }],
        }
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        self.assertEqual(ps3.strength, "supporting")
        self.assertEqual(ps3.confidence, "Low")

    def test_found_but_no_relevant_call_is_not_evaluated(self):
        """found=True but the only record is BS3 -- PS3 must not be
        fabricated as triggered, and must not silently reuse BS3's data."""
        functional_evidence_result = {
            "found": True, "source": "mavedb", "gene_symbol": "BRCA1",
            "records": [{"source": "mavedb", "call": "BS3", "strength": "moderate", "matched_hgvs": "x"}],
        }
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        self.assertEqual(ps3.status, "not_evaluated")

    def test_cftr_style_gap_end_to_end(self):
        """The real, live-confirmed CFTR gap: neither source has
        anything, so both criteria stay honestly not_evaluated."""
        functional_evidence_result = {"found": False, "source": "none", "gene_symbol": "CFTR", "records": []}
        ps3 = ACMGRuleEngine._ps3(functional_evidence_result)
        bs3 = ACMGRuleEngine._bs3(functional_evidence_result)
        self.assertEqual(ps3.status, "not_evaluated")
        self.assertEqual(bs3.status, "not_evaluated")

    def test_ps3_bs3_registered_in_full_engine_evaluate(self):
        """Confirms PS3/BS3 are wired into `evaluate()` itself (not
        just callable as standalone static methods) and no longer fall
        into the old blanket 'not integrated' stub message."""
        functional_evidence_result = {
            "found": True, "source": "clingen_erepo", "gene_symbol": "BRCA1",
            "records": [{"source": "clingen_erepo", "call": "PS3", "strength": "strong", "matched_hgvs": "x"}],
        }
        result = ACMGRuleEngine().evaluate(functional_evidence_result=functional_evidence_result)
        ps3 = result["all_criteria"]["PS3"]
        self.assertEqual(ps3["status"], "triggered")
        self.assertNotIn("not integrated", ps3["rationale"])

    def test_evaluate_with_no_functional_evidence_arg_is_backward_compatible(self):
        """Calling evaluate() with zero args (the old convention) must
        still work and report PS3/BS3 as not_evaluated, never raise."""
        result = ACMGRuleEngine().evaluate()
        self.assertEqual(result["all_criteria"]["PS3"]["status"], "not_evaluated")
        self.assertEqual(result["all_criteria"]["BS3"]["status"], "not_evaluated")


# ---------------------------------------------------------------------------
# report/json_builder.py -- backward compatibility for the new key
# ---------------------------------------------------------------------------

class TestJsonBuilderFunctionalEvidenceBackwardCompatibility(unittest.TestCase):
    def test_omitting_functional_evidence_result_still_produces_a_complete_record(self):
        from report.json_builder import build_variant_result

        result = build_variant_result(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
            sequence_context={}, dna_model_results={}, rna_result={}, protein_result={},
            blast_result={}, clinvar_result={}, dbsnp_result={}, interpretation={}, errors=[],
        )
        self.assertIn("functional_evidence", result)
        self.assertEqual(result["functional_evidence"], {"skipped": True, "found": False})

    def test_passing_functional_evidence_result_is_included_verbatim(self):
        from report.json_builder import build_variant_result

        payload = {"skipped": False, "found": True, "source": "clingen_erepo", "records": []}
        result = build_variant_result(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
            sequence_context={}, dna_model_results={}, rna_result={}, protein_result={},
            blast_result={}, clinvar_result={}, dbsnp_result={}, interpretation={}, errors=[],
            functional_evidence_result=payload,
        )
        self.assertEqual(result["functional_evidence"], payload)

    def test_omitting_it_does_not_disturb_clingen_or_other_keys(self):
        from report.json_builder import build_variant_result

        clingen_payload = {"skipped": False, "found": True, "gene_symbol": "BRCA1"}
        result = build_variant_result(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
            sequence_context={}, dna_model_results={}, rna_result={}, protein_result={},
            blast_result={}, clinvar_result={}, dbsnp_result={}, interpretation={}, errors=[],
            clingen_result=clingen_payload,
        )
        self.assertEqual(result["clingen"], clingen_payload)
        self.assertEqual(result["functional_evidence"], {"skipped": True, "found": False})
        for expected_key in (
            "variant", "blast", "clinvar", "dbsnp", "clingen", "functional_evidence", "interpretation", "errors",
        ):
            self.assertIn(expected_key, result)


# ---------------------------------------------------------------------------
# pipeline/orchestrator.py -- stage wiring
# ---------------------------------------------------------------------------

class TestOrchestratorFunctionalEvidenceStage(unittest.TestCase):
    """
    Exercises `GeperPipeline._run_functional_evidence_stage` directly
    against a mocked `functional_evidence_client`, the same
    `__new__`-bypass pattern `TestOrchestratorClinGenStage` uses.
    """

    def setUp(self):
        import _fake_heavy_deps

        _fake_heavy_deps.install()
        import importlib

        self.orchestrator_module = importlib.import_module("pipeline.orchestrator")

    def _make_bare_pipeline(self):
        pipeline = self.orchestrator_module.GeperPipeline.__new__(self.orchestrator_module.GeperPipeline)
        pipeline.sequence_context_gen = mock.Mock(assembly="GRCh38")
        pipeline._timer = mock.MagicMock()
        pipeline._timer.return_value.__enter__ = mock.Mock(return_value=None)
        pipeline._timer.return_value.__exit__ = mock.Mock(return_value=False)
        return pipeline

    def _variant(self):
        return mock.Mock(chrom="17", pos=43106534, ref="C", alt="A")

    def test_stage_returns_client_result_on_success(self):
        pipeline = self._make_bare_pipeline()
        pipeline.functional_evidence_client = mock.Mock()
        pipeline.functional_evidence_client.query_variant.return_value = {
            "found": True, "skipped": False, "source": "clingen_erepo", "records": [],
        }
        with mock.patch("pipeline.orchestrator.transcript_from_result", return_value=None):
            errors: list = []
            result = pipeline._run_functional_evidence_stage(
                self._variant(), {"gene_symbol": "BRCA1"}, {}, errors,
            )
        self.assertEqual(result["source"], "clingen_erepo")
        self.assertEqual(errors, [])
        pipeline.functional_evidence_client.query_variant.assert_called_once()
        _, kwargs = pipeline.functional_evidence_client.query_variant.call_args
        self.assertEqual(kwargs["gene_symbol"], "BRCA1")
        self.assertEqual(kwargs["hgvs_g"], "NC_000017.11:g.43106534C>A")
        self.assertIsNone(kwargs["hgvs_c"])

    def test_stage_records_error_without_raising_when_client_reports_error(self):
        pipeline = self._make_bare_pipeline()
        pipeline.functional_evidence_client = mock.Mock()
        pipeline.functional_evidence_client.query_variant.return_value = {"found": False, "error": "network unreachable"}
        with mock.patch("pipeline.orchestrator.transcript_from_result", return_value=None):
            errors: list = []
            result = pipeline._run_functional_evidence_stage(self._variant(), {}, {}, errors)
        self.assertEqual(result["error"], "network unreachable")
        self.assertEqual(len(errors), 1)
        self.assertIn("Functional-evidence", errors[0])

    def test_stage_never_raises_even_if_client_itself_raises(self):
        pipeline = self._make_bare_pipeline()
        pipeline.functional_evidence_client = mock.Mock()
        pipeline.functional_evidence_client.query_variant.side_effect = RuntimeError("unexpected bug")
        with mock.patch("pipeline.orchestrator.transcript_from_result", return_value=None):
            errors: list = []
            result = pipeline._run_functional_evidence_stage(self._variant(), {}, {}, errors)  # must not raise
        self.assertFalse(result["found"])
        self.assertIn("Functional-evidence (PS3/BS3) stage failed", errors[0])

    def test_orchestrator_constructs_functional_evidence_client(self):
        """`GeperPipeline.__init__` must wire up `self.functional_evidence_client`."""
        import inspect

        source = inspect.getsource(self.orchestrator_module.GeperPipeline.__init__)
        self.assertIn("self.functional_evidence_client", source)

    def test_process_variant_threads_functional_evidence_through(self):
        """`_process_variant` must call the new stage and pass its
        result into both `interpret()` and `build_variant_result()`."""
        import inspect

        source = inspect.getsource(self.orchestrator_module.GeperPipeline._process_variant)
        self.assertIn("_run_functional_evidence_stage", source)
        self.assertIn("functional_evidence_result=functional_evidence_result", source)


if __name__ == "__main__":
    unittest.main()
