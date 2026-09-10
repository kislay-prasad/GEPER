"""
Tests for patient-phenotype (HPO term) CLI input:
`pipeline/hpo/utils.py`'s `--hpo-terms` / `--phenotype-file` parsing
and validation helpers, and their wiring into `GeperPipeline` so ACMG's
PP4 rule (`pipeline/acmg_rules.py::ACMGRuleEngine._pp4`) can actually
receive a `phenotype_result` instead of always defaulting to `None`.

PP4's own rule logic (overlap/single-etiology thresholds) is already
thoroughly covered against real FBN1/CFTR fixture data in
`tests/test_hpo.py` -- this file covers the new input path (CLI
parsing/validation, and the orchestrator wiring point) and confirms
the parsed output feeds the real `ACMGRuleEngine` correctly end to end.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.hpo.models import HPOGeneEvidence, HPOPhenotypeAssociation
from pipeline.hpo.utils import (
    build_phenotype_result,
    is_well_formed_hpo_id,
    load_phenotype_file,
    parse_hpo_terms_arg,
)


class TestIsWellFormedHpoId(unittest.TestCase):
    def test_valid_id(self):
        self.assertTrue(is_well_formed_hpo_id("HP:0001250"))

    def test_missing_prefix_is_invalid(self):
        self.assertFalse(is_well_formed_hpo_id("0001250"))

    def test_wrong_digit_count_is_invalid(self):
        self.assertFalse(is_well_formed_hpo_id("HP:12345"))

    def test_empty_or_none_is_invalid(self):
        self.assertFalse(is_well_formed_hpo_id(""))
        self.assertFalse(is_well_formed_hpo_id(None))


class TestParseHpoTermsArg(unittest.TestCase):
    def test_none_returns_empty_list(self):
        self.assertEqual(parse_hpo_terms_arg(None), [])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(parse_hpo_terms_arg(""), [])

    def test_valid_comma_separated_terms(self):
        terms = parse_hpo_terms_arg("HP:0001250,HP:0002011")
        self.assertEqual(terms, ["HP:0001250", "HP:0002011"])

    def test_whitespace_around_terms_is_stripped(self):
        terms = parse_hpo_terms_arg(" HP:0001250 , HP:0002011 ")
        self.assertEqual(terms, ["HP:0001250", "HP:0002011"])

    def test_duplicate_terms_are_deduplicated(self):
        terms = parse_hpo_terms_arg("HP:0001250,HP:0001250")
        self.assertEqual(terms, ["HP:0001250"])

    def test_malformed_term_is_dropped_with_a_warning_not_a_crash(self):
        logger = mock.Mock()
        terms = parse_hpo_terms_arg("HP:0001250,not-an-hpo-id,HP:0002011", logger=logger)
        self.assertEqual(terms, ["HP:0001250", "HP:0002011"])
        logger.warning.assert_called_once()
        self.assertIn("not-an-hpo-id", logger.warning.call_args[0][0])

    def test_all_malformed_returns_empty_list_without_raising(self):
        terms = parse_hpo_terms_arg("garbage,also-garbage")
        self.assertEqual(terms, [])


class TestLoadPhenotypeFile(unittest.TestCase):
    def test_one_term_per_line_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("HP:0001250\nHP:0002011\n")
            self.assertEqual(load_phenotype_file(path), ["HP:0001250", "HP:0002011"])

    def test_json_list_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(["HP:0001250", "HP:0002011"], fh)
            self.assertEqual(load_phenotype_file(path), ["HP:0001250", "HP:0002011"])

    def test_malformed_json_warns_and_returns_empty_list(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[not valid json")
            result = load_phenotype_file(path, logger=logger)
        self.assertEqual(result, [])
        logger.warning.assert_called_once()

    def test_json_file_not_a_list_warns_and_returns_empty_list(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"not": "a list"}, fh)
            result = load_phenotype_file(path, logger=logger)
        self.assertEqual(result, [])
        logger.warning.assert_called_once()

    def test_missing_file_warns_and_returns_empty_list_without_raising(self):
        logger = mock.Mock()
        result = load_phenotype_file(os.path.join(tempfile.gettempdir(), "no-such-phenotypes-file.txt"), logger=logger)
        self.assertEqual(result, [])
        logger.warning.assert_called_once()

    def test_malformed_term_in_file_is_dropped_with_a_warning(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("HP:0001250\nnot-an-hpo-id\n")
            result = load_phenotype_file(path, logger=logger)
        self.assertEqual(result, ["HP:0001250"])
        logger.warning.assert_called_once()


class TestBuildPhenotypeResult(unittest.TestCase):
    def test_neither_input_returns_none(self):
        """The critical backward-compatibility guarantee: with no CLI
        phenotype input at all, `phenotype_result` must be exactly
        `None`, identical to every run before this feature existed, so
        PP4 continues to report 'not_evaluated'."""
        self.assertIsNone(build_phenotype_result(None, None))

    def test_hpo_terms_only(self):
        result = build_phenotype_result("HP:0001250,HP:0002011", None)
        self.assertEqual(result, {"hpo_term_ids": ["HP:0001250", "HP:0002011"]})

    def test_phenotype_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("HP:0001250\n")
            result = build_phenotype_result(None, path)
        self.assertEqual(result, {"hpo_term_ids": ["HP:0001250"]})

    def test_combines_and_dedupes_both_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phenotypes.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("HP:0001250\nHP:0009999\n")
            result = build_phenotype_result("HP:0001250,HP:0002011", path)
        self.assertEqual(result, {"hpo_term_ids": ["HP:0001250", "HP:0002011", "HP:0009999"]})

    def test_only_malformed_terms_still_returns_a_dict_not_none(self):
        """An operator who *did* pass --hpo-terms, even if every term
        turned out malformed, gets an explicit empty-list dict (still
        behaves identically to None for PP4) rather than silently
        reverting to the "nothing supplied" None sentinel."""
        result = build_phenotype_result("garbage-term", None)
        self.assertEqual(result, {"hpo_term_ids": []})


def _fbn1_marfan_only_evidence_dict():
    """Small, hand-built HPO gene-evidence fixture: FBN1, curated
    against exactly one disease (Marfan syndrome, OMIM:154700) with two
    real Marfan phenotype terms -- enough to drive PP4's overlap /
    single-etiology thresholds without depending on the larger real
    FBN1/CFTR fixture file `tests/test_hpo.py` uses."""
    associations = [
        HPOPhenotypeAssociation(
            gene_symbol="FBN1", hpo_id="HP:0001166", hpo_name="Arachnodactyly", disease_id="OMIM:154700"
        ),
        HPOPhenotypeAssociation(
            gene_symbol="FBN1", hpo_id="HP:0000098", hpo_name="Tall stature", disease_id="OMIM:154700"
        ),
    ]
    return HPOGeneEvidence(
        gene_symbol="FBN1",
        source="local_dataset",
        found=True,
        phenotype_associations=associations,
        ncbi_gene_id="2200",
    ).to_dict()


class TestBuildPhenotypeResultFeedsPP4EndToEnd(unittest.TestCase):
    """Confirms the new CLI-facing parsing produces exactly the dict
    shape PP4 (see tests/test_hpo.py) already expects, by running it
    through the real, unmodified `ACMGRuleEngine` -- covering the four
    behaviors requested: no terms (unchanged), matching terms
    (triggers), non-matching terms (does not trigger)."""

    def test_no_terms_supplied_is_not_evaluated(self):
        """Unchanged behavior: identical to every run before this feature."""
        phenotype_result = build_phenotype_result(None, None)
        result = ACMGRuleEngine().evaluate(
            phenotype_result=phenotype_result,
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "not_evaluated")
        self.assertIn("none were supplied for this run", pp4["rationale"])

    def test_matching_terms_trigger_pp4(self):
        phenotype_result = build_phenotype_result("HP:0001166,HP:0000098", None)
        result = ACMGRuleEngine().evaluate(
            phenotype_result=phenotype_result,
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["strength"], "supporting")

    def test_non_matching_terms_do_not_trigger_pp4(self):
        phenotype_result = build_phenotype_result("HP:9999999,HP:8888888", None)
        result = ACMGRuleEngine().evaluate(
            phenotype_result=phenotype_result,
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        pp4 = result["all_criteria"]["PP4"]
        self.assertEqual(pp4["status"], "not_triggered")
        self.assertEqual(pp4["details"]["overlap_count"], 0)

    def test_malformed_terms_are_dropped_leaving_no_valid_patient_terms_so_not_evaluated(self):
        """A phenotype file/flag containing only malformed IDs must
        warn (already covered above) and never crash -- and here,
        since no valid patient term survives validation, PP4 correctly
        falls back to 'not_evaluated' rather than crashing or silently
        triggering on empty evidence."""
        logger = mock.Mock()
        phenotype_result = build_phenotype_result("not-an-hpo-id", None, logger=logger)
        result = ACMGRuleEngine().evaluate(
            phenotype_result=phenotype_result,
            hpo_result=_fbn1_marfan_only_evidence_dict(),
        )
        self.assertEqual(result["all_criteria"]["PP4"]["status"], "not_evaluated")
        logger.warning.assert_called_once()


class TestOrchestratorWiring(unittest.TestCase):
    """White-box check (same convention as
    test_ai_model_orchestration.py's
    test_orchestrator_init_constructs_model_manager_and_ensemble_manager)
    that `GeperPipeline` actually accepts and threads `phenotype_result`
    through to `InterpretationEngine.interpret(...)` -- the one missing
    link identified before this feature (every layer below it, down to
    `_pp4`, already accepted `phenotype_result`)."""

    def setUp(self):
        import _fake_heavy_deps

        _fake_heavy_deps.install()

    def test_init_accepts_and_stores_phenotype_result(self):
        import inspect

        from pipeline.orchestrator import GeperPipeline

        signature = inspect.signature(GeperPipeline.__init__)
        self.assertIn("phenotype_result", signature.parameters)
        self.assertIsNone(signature.parameters["phenotype_result"].default)

        source = inspect.getsource(GeperPipeline.__init__)
        self.assertIn("self.phenotype_result = phenotype_result", source)

    def test_process_variant_passes_phenotype_result_to_interpret(self):
        import inspect

        from pipeline.orchestrator import GeperPipeline

        source = inspect.getsource(GeperPipeline._process_variant)
        self.assertIn("phenotype_result=self.phenotype_result", source)


if __name__ == "__main__":
    unittest.main()
