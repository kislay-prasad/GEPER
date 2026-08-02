"""
Tests for case-level, HPO-phenotype-driven variant ranking
(`pipeline/case_prioritization.py`, `pipeline/hpo/ontology.py`, and
`pipeline/orchestrator.py::GeperPipeline._apply_case_phenotype_ranking`).

Lightweight and offline by design: `HPOOntology` is built from small,
hand-constructed Obograph-JSON fixtures (not HPO's real ~19MB release --
see `pipeline/hpo/ontology.py`'s own docstring for why that file is
never required for this feature to work), and every "gene HPO result"
here is a plain dict matching `pipeline/hpo/lookup.py::HPOLookup`'s
real return shape, not a live lookup. No model weights, no network, no
real pipeline run -- exactly what this task's constraints require.

Gene choices in the multi-variant scenarios below (BRCA1, TP53, PRNP)
match `geper/test_data/nuclear_test.vcf`'s own variants, so this
exercises the same genes a real Colab run against that file would.
"""

import unittest

from pipeline.case_prioritization import rank_case, score_phenotype_match
from pipeline.hpo.ontology import HPOOntology


def _obograph(edges):
    """Builds a minimal Obograph-JSON dict from a list of (child_short_id, parent_short_id) pairs, e.g. ('HP:0002133', 'HP:0001250')."""
    return {
        "graphs": [
            {
                "edges": [
                    {
                        "sub": f"http://purl.obolibrary.org/obo/{c.replace(':', '_')}",
                        "pred": "is_a",
                        "obj": f"http://purl.obolibrary.org/obo/{p.replace(':', '_')}",
                    }
                    for c, p in edges
                ]
            }
        ]
    }


# A small, realistic slice of real HPO structure (verified against real
# HPO term IDs/labels, not invented): neurological-phenotype branch
# used for the PRNP (prion disease) scenario below.
#   HP:0100022 Abnormality of movement
#     -> HP:0001337 Tremor
#   HP:0000707 Abnormality of the nervous system
#     -> HP:0012638 Abnormal nervous system physiology
#          -> HP:0000726 Dementia
#          -> HP:0002185 Neurodegeneration
#               -> HP:0007359 Focal-onset seizure  (invented child, for a 2-hop-vs-1-hop test)
_NEURO_ONTOLOGY = _obograph(
    [
        ("HP:0012638", "HP:0000707"),
        ("HP:0000726", "HP:0012638"),
        ("HP:0002185", "HP:0012638"),
        ("HP:0007359", "HP:0002185"),
    ]
)


class HPOOntologyTests(unittest.TestCase):
    def test_parses_is_a_edges_and_computes_transitive_ancestors(self):
        ont = HPOOntology(graph_data=_NEURO_ONTOLOGY)
        self.assertTrue(ont.is_available)
        # HP:0007359 -> HP:0002185 -> HP:0012638 -> HP:0000707 (transitive closure)
        self.assertEqual(
            ont.ancestors("HP:0007359"),
            frozenset({"HP:0002185", "HP:0012638", "HP:0000707"}),
        )

    def test_empty_graph_data_is_unavailable(self):
        ont = HPOOntology(graph_data={"graphs": [{"edges": []}]})
        self.assertFalse(ont.is_available)
        self.assertEqual(ont.ancestors("HP:0000001"), frozenset())

    def test_no_graph_data_at_all_is_unavailable(self):
        ont = HPOOntology()
        self.assertFalse(ont.is_available)

    def test_malformed_graph_data_degrades_instead_of_raising(self):
        ont = HPOOntology(graph_data={"graphs": "not a list"})
        self.assertFalse(ont.is_available)

    def test_non_is_a_edges_are_ignored(self):
        data = {
            "graphs": [
                {
                    "edges": [
                        {
                            "sub": "http://purl.obolibrary.org/obo/HP_0000001",
                            "pred": "part_of",
                            "obj": "http://purl.obolibrary.org/obo/HP_0000002",
                        },
                    ]
                }
            ]
        }
        ont = HPOOntology(graph_data=data)
        self.assertFalse(ont.is_available)

    def test_unknown_term_returns_empty_ancestors_not_error(self):
        ont = HPOOntology(graph_data=_NEURO_ONTOLOGY)
        self.assertEqual(ont.ancestors("HP:9999999"), frozenset())

    def test_from_file_missing_path_is_unavailable_not_raising(self):
        ont = HPOOntology.from_file("/nonexistent/path/hp.json")
        self.assertFalse(ont.is_available)
        ont2 = HPOOntology.from_file(None)
        self.assertFalse(ont2.is_available)


class ScorePhenotypeMatchTests(unittest.TestCase):
    def setUp(self):
        self.ontology = HPOOntology(graph_data=_NEURO_ONTOLOGY)
        self.no_ontology = HPOOntology()

    def test_exact_match_scores_100(self):
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [{"hpo_id": "HP:0002185", "hpo_name": "Neurodegeneration"}],
        }
        result = score_phenotype_match(["HP:0002185"], gene_hpo, self.ontology)
        self.assertEqual(result.score, 100.0)
        self.assertTrue(result.gene_hpo_available)
        self.assertEqual(result.term_matches[0].match_type, "exact")

    def test_ancestor_partial_credit_between_zero_and_hundred(self):
        # patient observed the CHILD term (focal-onset seizure); gene is
        # curated with the PARENT (neurodegeneration) -- shares real
        # ontological structure, not an exact ID match.
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [{"hpo_id": "HP:0002185", "hpo_name": "Neurodegeneration"}],
        }
        result = score_phenotype_match(["HP:0007359"], gene_hpo, self.ontology)
        self.assertIsNotNone(result.score)
        self.assertGreater(result.score, 0.0)
        self.assertLess(result.score, 100.0)
        self.assertEqual(result.term_matches[0].match_type, "ancestor")
        self.assertEqual(result.term_matches[0].matched_gene_term_id, "HP:0002185")

    def test_unrelated_terms_score_zero_even_with_ontology(self):
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [{"hpo_id": "HP:0001337", "hpo_name": "Tremor"}],
        }
        # HP:0007359 (seizure branch) shares no ancestor with HP:0001337
        # (movement branch) in this small fixture graph.
        result = score_phenotype_match(["HP:0007359"], gene_hpo, self.ontology)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.term_matches[0].match_type, "none")

    def test_no_ontology_falls_back_to_exact_match_only(self):
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [{"hpo_id": "HP:0002185", "hpo_name": "Neurodegeneration"}],
        }
        result = score_phenotype_match(["HP:0007359"], gene_hpo, self.no_ontology)
        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.ontology_available)
        self.assertIn("exact-ID match only", result.reason)

    def test_no_gene_hpo_data_returns_none_score_not_zero(self):
        result = score_phenotype_match(["HP:0002185"], {"found": False, "skipped": False}, self.no_ontology)
        self.assertIsNone(result.score)
        self.assertFalse(result.gene_hpo_available)
        self.assertIn("No HPO-curated phenotype data", result.reason)

    def test_gene_hpo_skipped_or_errored_returns_none_score(self):
        for bad_result in ({"skipped": True}, {"found": True, "error": "boom"}, None):
            with self.subTest(bad_result=bad_result):
                result = score_phenotype_match(["HP:0002185"], bad_result, self.no_ontology)
                self.assertIsNone(result.score)

    def test_multiple_patient_terms_averaged_via_best_match_per_term(self):
        # One patient term matches exactly, one has no gene-term match at
        # all -- score should be the average of [1.0, 0.0] * 100, not
        # diluted by every gene term individually.
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [
                {"hpo_id": "HP:0002185", "hpo_name": "Neurodegeneration"},
                {"hpo_id": "HP:0001337", "hpo_name": "Tremor"},
            ],
        }
        result = score_phenotype_match(["HP:0002185", "HP:9999998"], gene_hpo, self.no_ontology)
        self.assertAlmostEqual(result.score, 50.0)
        self.assertEqual(len(result.term_matches), 2)

    def test_ancestor_similarity_below_threshold_treated_as_no_match(self):
        # A tiny "everything descends from one root" graph, where the
        # only shared ancestor is the near-universal root -- Jaccard
        # similarity should be small enough to fall below
        # MIN_ANCESTOR_SIMILARITY and register as no match, not noise.
        broad_graph = _obograph(
            [
                ("HP:0001111", "HP:0000001"),
                ("HP:0002222", "HP:0000001"),
            ]
        )
        ont = HPOOntology(graph_data=broad_graph)
        gene_hpo = {
            "found": True,
            "skipped": False,
            "distinct_phenotype_terms": [{"hpo_id": "HP:0002222", "hpo_name": "X"}],
        }

        class _StrictCfg:
            MIN_ANCESTOR_SIMILARITY = 0.9  # deliberately strict for this test

        result = score_phenotype_match(["HP:0001111"], gene_hpo, ont, cfg=_StrictCfg())
        self.assertEqual(result.score, 0.0)


class RankCaseTests(unittest.TestCase):
    """
    Realistic multi-variant scenarios mirroring
    geper/test_data/nuclear_test.vcf's genes (BRCA1, TP53, PRNP),
    mocked exactly as `pipeline/orchestrator.py::_apply_case_phenotype_ranking`
    would build its inputs from each variant's already-computed
    `hpo`/`interpretation_result` dicts -- never a real pipeline run.
    """

    def setUp(self):
        self.ontology = (
            HPOOntology()
        )  # exact-match-only; keeps this suite's expectations independent of ontology partial credit

    def test_no_variants_returns_empty_list(self):
        self.assertEqual(rank_case([], [], ["HP:0003002"], self.ontology), [])

    def test_no_patient_terms_still_runs_but_scores_zero(self):
        # Guarding "don't run at all with no HPO terms" is the
        # orchestrator's job (see `GeperPipeline.run`'s
        # `if self.phenotype_result and ...` gate) -- this function
        # itself just needs to not crash if ever called with an empty
        # term list (e.g. a malformed/empty --phenotype-file).
        hpo = [
            {
                "found": True,
                "skipped": False,
                "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "Breast carcinoma"}],
            }
        ]
        results = rank_case(hpo, [50.0], [], self.ontology)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].phenotype_match.score, 0.0)

    def test_realistic_three_gene_scenario_ranks_by_phenotype_then_priority(self):
        # Patient presents with breast-cancer-related symptoms.
        patient_terms = ["HP:0003002", "HP:0100615"]  # Breast carcinoma, Ovarian neoplasm

        # Variant 0: BRCA1, exact phenotype match on both terms, MODERATE priority.
        # Variant 1: TP53, one exact match (Breast carcinoma is a real TP53/Li-Fraumeni phenotype), HIGH priority.
        # Variant 2: PRNP, no phenotype overlap at all, HIGHEST priority (should still rank behind real phenotype matches).
        hpo_results = [
            {
                "found": True,
                "skipped": False,
                "distinct_phenotype_terms": [
                    {"hpo_id": "HP:0003002", "hpo_name": "Breast carcinoma"},
                    {"hpo_id": "HP:0100615", "hpo_name": "Ovarian neoplasm"},
                ],
            },
            {
                "found": True,
                "skipped": False,
                "distinct_phenotype_terms": [
                    {"hpo_id": "HP:0003002", "hpo_name": "Breast carcinoma"},
                    {"hpo_id": "HP:0002664", "hpo_name": "Neoplasm"},
                ],
            },
            {
                "found": True,
                "skipped": False,
                "distinct_phenotype_terms": [
                    {"hpo_id": "HP:0002185", "hpo_name": "Neurodegeneration"},
                    {"hpo_id": "HP:0000726", "hpo_name": "Dementia"},
                ],
            },
        ]
        priority_scores = [55.0, 80.0, 97.0]

        results = rank_case(hpo_results, priority_scores, patient_terms, self.ontology)
        self.assertEqual(len(results), 3)

        by_rank = {r.case_rank: i for i, r in enumerate(results)}
        # BRCA1 (both terms exact) must outrank TP53 (one term exact),
        # and both must outrank PRNP (zero overlap) despite PRNP having
        # by far the highest raw priority score.
        self.assertLess(by_rank[1], by_rank[2])
        self.assertEqual(results[by_rank[1]].tier, "phenotype_matched")
        brca1_idx = 0
        self.assertEqual(by_rank[results[brca1_idx].case_rank], brca1_idx)
        prnp_result = results[2]
        self.assertEqual(
            prnp_result.case_rank,
            3,
            "PRNP has zero phenotype overlap and must rank last despite the highest priority score.",
        )
        self.assertEqual(
            prnp_result.tier, "phenotype_matched"
        )  # gene HAS hpo data, just no overlap -- distinct from "no_gene_hpo_data"

    def test_gene_with_no_hpo_data_ranks_last_with_clear_reason_not_first(self):
        # Requirement: "a variant's gene has no HPO data -> rank last
        # with a clear reason, not silently at position 1." Give the
        # no-data variant the HIGHEST priority score to make sure a
        # naive priority-only sort (which WOULD put it first) is not
        # what happens.
        hpo_results = [
            {
                "found": True,
                "skipped": False,
                "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "Breast carcinoma"}],
            },
            {"found": False, "skipped": False},  # no HPO curation at all for this gene
        ]
        priority_scores = [10.0, 99.0]
        results = rank_case(hpo_results, priority_scores, ["HP:0003002"], self.ontology)

        no_data_result = next(r for r in results if r.tier == "no_gene_hpo_data")
        matched_result = next(r for r in results if r.tier == "phenotype_matched")
        self.assertGreater(no_data_result.case_rank, matched_result.case_rank)
        self.assertIsNone(no_data_result.case_rank_score)
        self.assertIn("No HPO-curated phenotype data", no_data_result.reason)
        self.assertIn("Ranked after every phenotype-matched variant", no_data_result.reason)

    def test_variant_with_no_priority_score_is_not_scored_not_fabricated(self):
        hpo_results = [
            {"found": True, "skipped": False, "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "X"}]}
        ]
        results = rank_case(hpo_results, [None], ["HP:0003002"], self.ontology)
        self.assertEqual(results[0].tier, "not_scored")
        self.assertIsNone(results[0].case_rank)
        self.assertIsNone(results[0].case_rank_score)

    def test_tied_scores_preserve_original_order(self):
        hpo_results = [
            {"found": True, "skipped": False, "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "X"}]},
            {"found": True, "skipped": False, "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "X"}]},
        ]
        results = rank_case(hpo_results, [50.0, 50.0], ["HP:0003002"], self.ontology)
        self.assertEqual(results[0].case_rank, 1)
        self.assertEqual(results[1].case_rank, 2)

    def test_result_shape_serializes_cleanly(self):
        hpo_results = [
            {"found": True, "skipped": False, "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "X"}]}
        ]
        results = rank_case(hpo_results, [50.0], ["HP:0003002"], self.ontology)
        d = results[0].to_dict()
        self.assertIn("case_rank", d)
        self.assertIn("phenotype_match", d)
        self.assertIsInstance(d["phenotype_match"], dict)


class OrchestratorWiringTests(unittest.TestCase):
    """
    Exercises the actual orchestrator glue
    (`_apply_case_phenotype_ranking`) without constructing a real
    `GeperPipeline` (which would load real models/config for the
    full pipeline) -- calls the unbound method against a minimal stand-in
    object carrying only the one attribute it reads
    (`phenotype_result`), and monkeypatches `get_shared_ontology` so
    this never attempts a real network fetch.
    """

    def test_mutates_variant_results_in_place_with_case_prioritization_key(self):
        from unittest import mock

        import pipeline.orchestrator as orch_module

        class _FakeSelf:
            phenotype_result = {"hpo_term_ids": ["HP:0003002"]}

        variant_results = [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "hpo": {
                    "found": True,
                    "skipped": False,
                    "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "Breast carcinoma"}],
                },
                "interpretation_result": {"gene_symbol": "BRCA1", "priority_score": 70.0},
            },
            {
                "variant": {"chrom": "20", "pos": 4699525, "ref": "C", "alt": "T"},
                "hpo": {"found": False, "skipped": False},
                "interpretation_result": {"gene_symbol": "PRNP", "priority_score": 40.0},
            },
        ]

        with mock.patch.object(orch_module, "get_shared_ontology", return_value=HPOOntology()):
            orch_module.GeperPipeline._apply_case_phenotype_ranking(_FakeSelf(), variant_results)

        self.assertIn("case_prioritization", variant_results[0])
        self.assertIn("case_prioritization", variant_results[1])
        self.assertEqual(variant_results[0]["case_prioritization"]["case_rank"], 1)
        self.assertEqual(variant_results[1]["case_prioritization"]["tier"], "no_gene_hpo_data")
        # Must never touch the existing, separate keys.
        self.assertNotIn("case_prioritization", variant_results[0]["interpretation_result"])
        self.assertEqual(variant_results[0]["interpretation_result"]["priority_score"], 70.0)

    def test_variant_with_errored_interpretation_result_treated_as_no_priority_score(self):
        from unittest import mock

        import pipeline.orchestrator as orch_module

        class _FakeSelf:
            phenotype_result = {"hpo_term_ids": ["HP:0003002"]}

        variant_results = [
            {
                "variant": {"chrom": "1", "pos": 1, "ref": "A", "alt": "G"},
                "hpo": {
                    "found": True,
                    "skipped": False,
                    "distinct_phenotype_terms": [{"hpo_id": "HP:0003002", "hpo_name": "X"}],
                },
                "interpretation_result": {"error": "boom"},
            },
        ]
        with mock.patch.object(orch_module, "get_shared_ontology", return_value=HPOOntology()):
            orch_module.GeperPipeline._apply_case_phenotype_ranking(_FakeSelf(), variant_results)

        self.assertEqual(variant_results[0]["case_prioritization"]["tier"], "not_scored")


if __name__ == "__main__":
    unittest.main()
