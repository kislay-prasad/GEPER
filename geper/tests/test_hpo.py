"""
Tests for the HPO gene-phenotype evidence integration
(`pipeline/hpo/`) and the ACMG/AMP PP4 rule
(`pipeline/acmg_rules.py::ACMGRuleEngine._pp4`).

Ground truth, not synthetic guesses: `tests/fixtures/hpo_genes_to_phenotype_fbn1_cftr.tsv`
is a frozen, real, verbatim slice (671 rows, FBN1 + CFTR only) of HPO's
official live Gene-to-Phenotype annotation release,
http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt (fetched
2026-07-29). Confirmed against HPO's published associations during
development, both through this same local-dataset path and
independently through the live Monarch/JAX API
(https://ontology.jax.org/api/network/annotation/{ncbi_gene_id}):
  - FBN1 (NCBIGene:2200) -> 537 rows, 312 distinct HPO terms, 17
    distinct HPO-curated disease entries, including the classic Marfan
    syndrome curation (OMIM:154700, 71 rows) with Arachnodactyly
    (HP:0001166, observed 124/197) and Tall stature (HP:0000098) among
    its phenotypes -- textbook Marfan features.
  - CFTR (NCBIGene:1080) -> 134 rows, including Cystic fibrosis
    (OMIM:219700 and the Orphanet duplicate ORPHA:586).

FBN1's real 17-distinct-disease count is also used below as a real,
not constructed, illustration of PP4's disclosed "single genetic
etiology" heuristic limitation (see `_pp4`'s own docstring): FBN1
genuinely causes more than one distinct fibrillinopathy (Marfan
syndrome, Weill-Marchesani syndrome 2, MASS phenotype, isolated ectopia
lentis, etc.), so the *whole-gene* HPO curation correctly fails
GEPER's disease-count proxy even though a *specific* presentation
narrowed to one of those diseases (filtered to just the Marfan rows,
also below) should not.
"""

import csv
import os
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.hpo.models import HPOGeneEvidence, HPOPhenotypeAssociation
from pipeline.hpo.provider import CompositeHPOProvider, LocalDatasetHPOProvider
from pipeline.hpo.utils import parse_genes_to_phenotype_row

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "hpo_genes_to_phenotype_fbn1_cftr.tsv")


def _local_provider():
    return LocalDatasetHPOProvider(local_file_path=_FIXTURE_PATH, auto_fetch=False)


def _load_raw_rows():
    with open(_FIXTURE_PATH, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


# ---------------------------------------------------------------------------
# Row parsing -- pure, real fixture rows
# ---------------------------------------------------------------------------

class TestParseGenesToPhenotypeRow(unittest.TestCase):
    def test_real_fbn1_marfan_arachnodactyly_row_parses(self):
        rows = _load_raw_rows()
        row = next(r for r in rows if r["gene_symbol"] == "FBN1" and r["hpo_id"] == "HP:0001166" and r["disease_id"] == "OMIM:154700")
        parsed = parse_genes_to_phenotype_row(row)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.gene_symbol, "FBN1")
        self.assertEqual(parsed.hpo_name, "Arachnodactyly")
        self.assertEqual(parsed.frequency, "124/197")
        self.assertEqual(parsed.disease_id, "OMIM:154700")
        self.assertEqual(parsed.ncbi_gene_id, "2200")

    def test_row_missing_required_field_returns_none(self):
        self.assertIsNone(parse_genes_to_phenotype_row({"gene_symbol": "FBN1", "hpo_id": "", "hpo_name": ""}))


# ---------------------------------------------------------------------------
# LocalDatasetHPOProvider -- real FBN1/CFTR ground truth
# ---------------------------------------------------------------------------

class TestLocalDatasetHPOProvider(unittest.TestCase):
    def test_fbn1_real_marfan_association_present(self):
        evidence = _local_provider().query("FBN1")
        self.assertTrue(evidence.found)
        self.assertEqual(evidence.source, "local_dataset")
        self.assertEqual(evidence.ncbi_gene_id, "2200")
        self.assertIn("OMIM:154700", evidence.distinct_disease_ids)
        term_ids = {t["hpo_id"] for t in evidence.distinct_phenotype_terms}
        self.assertIn("HP:0001166", term_ids)  # Arachnodactyly

    def test_fbn1_has_multiple_distinct_disease_entries(self):
        """Real: FBN1 causes more than one distinct fibrillinopathy -- see module docstring."""
        evidence = _local_provider().query("FBN1")
        self.assertGreater(len(evidence.distinct_disease_ids), 3)

    def test_cftr_real_cystic_fibrosis_association_present(self):
        evidence = _local_provider().query("CFTR")
        self.assertTrue(evidence.found)
        self.assertEqual(evidence.ncbi_gene_id, "1080")
        self.assertTrue({"OMIM:219700", "ORPHA:586"} & set(evidence.distinct_disease_ids))

    def test_gene_not_in_fixture_is_not_found(self):
        evidence = _local_provider().query("TP53")
        self.assertFalse(evidence.found)
        self.assertEqual(evidence.source, "local_dataset")

    def test_provider_unavailable_without_file_or_auto_fetch(self):
        provider = LocalDatasetHPOProvider(local_file_path=None, auto_fetch=False)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.query("FBN1"))


# ---------------------------------------------------------------------------
# CompositeHPOProvider -- local-first / API-fallback routing
# ---------------------------------------------------------------------------

class _StubProvider:
    def __init__(self, name, result):
        self.name = name
        self._result = result

    def query(self, gene_symbol):
        return self._result


class TestCompositeHPOProvider(unittest.TestCase):
    def test_local_hit_short_circuits_api(self):
        local_evidence = HPOGeneEvidence(gene_symbol="FBN1", source="local_dataset", found=True)
        api_provider = _StubProvider("api", HPOGeneEvidence.from_error("FBN1", "should not be called"))
        composite = CompositeHPOProvider(local_provider=_StubProvider("local_dataset", local_evidence), api_provider=api_provider)
        result = composite.query("FBN1")
        self.assertEqual(result.source, "local_dataset")

    def test_local_miss_falls_back_to_api(self):
        local_miss = HPOGeneEvidence.not_found("XYZ1", "local_dataset")
        api_hit = HPOGeneEvidence(gene_symbol="XYZ1", source="api", found=True)
        composite = CompositeHPOProvider(local_provider=_StubProvider("local_dataset", local_miss), api_provider=_StubProvider("api", api_hit))
        result = composite.query("XYZ1")
        self.assertEqual(result.source, "api")

    def test_neither_source_found_returns_not_found(self):
        composite = CompositeHPOProvider(
            local_provider=_StubProvider("local_dataset", HPOGeneEvidence.not_found("ZZZ", "local_dataset")),
            api_provider=_StubProvider("api", HPOGeneEvidence.not_found("ZZZ", "api")),
        )
        result = composite.query("ZZZ")
        self.assertFalse(result.found)


# ---------------------------------------------------------------------------
# PP4 -- real FBN1 ground truth, through the full rule engine
# ---------------------------------------------------------------------------

def _fbn1_evidence_dict():
    return _local_provider().query("FBN1").to_dict()


def _fbn1_marfan_only_evidence_dict():
    """Same real FBN1 fixture rows, filtered to only the Marfan syndrome (OMIM:154700) disease entry -- a real, not constructed, single-etiology-narrowed view."""
    rows = _load_raw_rows()
    marfan_rows = [r for r in rows if r["gene_symbol"] == "FBN1" and r["disease_id"] == "OMIM:154700"]
    associations = [parse_genes_to_phenotype_row(r) for r in marfan_rows]
    associations = [a for a in associations if a is not None]
    evidence = HPOGeneEvidence(gene_symbol="FBN1", source="local_dataset", found=True, phenotype_associations=associations, ncbi_gene_id="2200")
    return evidence.to_dict()


class TestPP4(unittest.TestCase):
    def test_no_phenotype_input_is_not_evaluated(self):
        """The default path when --hpo-terms/--phenotype-file are not passed for a run."""
        result = ACMGRuleEngine().evaluate(hpo_result=_fbn1_evidence_dict())
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "not_evaluated")
        self.assertIn("none were supplied for this run", pp4["rationale"])

    def test_phenotype_input_without_hpo_result_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(phenotype_result={"hpo_term_ids": ["HP:0001166"]})
        self.assertEqual(result["all_criteria"]["PP4"]["status"], "not_evaluated")

    def test_real_marfan_phenotype_against_marfan_only_view_triggers(self):
        """Real FBN1 fixture data, filtered to just its Marfan (OMIM:154700) rows -- high overlap, single disease -- triggers."""
        result = ACMGRuleEngine().evaluate(
            phenotype_result={"hpo_term_ids": ["HP:0001166", "HP:0000098"]},  # real: Arachnodactyly, Tall stature
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["strength"], "supporting")
        self.assertEqual(pp4["details"]["distinct_disease_count"], 1)

    def test_real_full_fbn1_gene_view_does_not_trigger_due_to_multi_etiology_heuristic(self):
        """Same real phenotype overlap, but against FBN1's whole-gene HPO curation (17 real distinct diseases) -- fails the single-etiology proxy, exactly the disclosed heuristic limitation."""
        result = ACMGRuleEngine().evaluate(
            phenotype_result={"hpo_term_ids": ["HP:0001166", "HP:0000098"]},
            hpo_result=_fbn1_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "not_triggered")
        self.assertIn("distinct HPO-curated disease entries", pp4["rationale"])

    def test_low_overlap_does_not_trigger(self):
        result = ACMGRuleEngine().evaluate(
            phenotype_result={"hpo_term_ids": ["HP:9999999", "HP:8888888"]},  # no real overlap with FBN1
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "not_triggered")
        self.assertEqual(pp4["details"]["overlap_count"], 0)

    def test_hpo_result_not_found_is_not_evaluated(self):
        result = ACMGRuleEngine().evaluate(
            phenotype_result={"hpo_term_ids": ["HP:0001166"]},
            hpo_result={"skipped": False, "found": False},
        )
        self.assertEqual(result["all_criteria"]["PP4"]["status"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
