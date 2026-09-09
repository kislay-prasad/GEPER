"""
tests/test_interpretation_outcome.py
─────────────────────────────────────

Human ruling (2026-09-09/10): the INTERPRETATION-OUTCOME state, a
per-variant judgement carried ALONGSIDE the ACMG classification, never
derived from the per-source retrieval states already in the engine
(`pipeline/stage_schemas.py::StageStatus`). Four values: interpreted,
insufficient_evidence, conflicting_evidence, review_required (the last
HELD -- no trigger set implemented in this commit, see the dispatch
report).

Declared BEFORE running, against master 2e50068b (before this
dispatch's production changes): EXPECTED RESULT was FAILURE --
`pipeline.interpretation_outcome` does not exist yet, so every test in
`TestDetermineInterpretationOutcomePureFunction` and
`TestAcmgRuleEngineNeverRelabelsAFailureAsInsufficiency` must fail at
collection/import time with `ModuleNotFoundError`, and
`TestInterpretationResultCarriesTheOutcome` must fail because
`InterpretationResult`/`build_interpretation_result` do not yet expose
`interpretation_outcome`. Confirmed failing (see the commit history:
the test-only commit on this branch precedes the production-code
commit) before any production code changed.

THE TEST THIS FILE EXISTS TO WRITE (dispatch's own words): "write the
test that would catch the defect this task exists to prevent: a query
that FAILS must not produce insufficient_evidence. Prove it by making a
source fail on purpose." That is
`TestAcmgRuleEngineNeverRelabelsAFailureAsInsufficiency::
test_a_source_failing_on_purpose_does_not_produce_insufficient_evidence`
below -- a real `ACMGRuleEngine().evaluate()` call, one evidence source
deliberately given a genuine `error` key (not a `found: False`, not a
skip), every other source and the gene resolution status otherwise
clean-empty, and the assertion is that `interpretation_outcome` is
`"interpreted"`, never `"insufficient_evidence"`.
"""

from __future__ import annotations

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpretation_outcome import InterpretationOutcome, determine_interpretation_outcome

_VARIANT = {"chrom": "1", "pos": 12345, "ref": "A", "alt": "G"}


def _evaluate(**overrides):
    """Minimal, all-clean-empty `ACMGRuleEngine.evaluate()` call --
    mirrors `test_mtdna_compartment_gate.py`'s own `_evaluate` helper
    shape, but for an ordinary nuclear variant (no mtDNA gating)."""
    kwargs = dict(
        clinvar_result={"records": []},
        dbsnp_result={"found": False},
        protein_result={},
        alphamissense_result={"skipped": True},
        mmsplice_result={"predicted": False},
        gnomad_result={"found": False},
        conservation_result={},
        clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        interpro_result={},
        ensemble_result={},
        variant_dict=_VARIANT,
        transcript_result={},
        clinvar_codon_result={},
        uniprot_result={},
        spliceformer_result={},
        splicebert_result={},
        hpo_result={},
        phenotype_result=None,
        functional_evidence_result={},
    )
    kwargs.update(overrides)
    return ACMGRuleEngine().evaluate(**kwargs)


class TestDetermineInterpretationOutcomePureFunction(unittest.TestCase):
    """The core of the task, tested in isolation from the engine: the
    definition must hold as a pure function, not only as an enum."""

    def test_no_evidence_at_all_with_a_clean_resolution_is_insufficient(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=True, pathogenic_points=0.0, benign_points=0.0
        )
        self.assertIs(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)

    def test_no_evidence_with_an_unresolved_gene_is_not_insufficient(self):
        """Gene never resolved -- the honest answer is NOT
        insufficient_evidence even though no criterion fired."""
        outcome = determine_interpretation_outcome(
            gene_resolved=False, evidence_queries_completed=True, pathogenic_points=0.0, benign_points=0.0
        )
        self.assertIsNot(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_no_evidence_with_a_query_failure_is_not_insufficient(self):
        """THE defect this task exists to prevent, at the pure-function
        level: a query that ran and failed must not be relabelled as a
        query that ran and found nothing."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=False, pathogenic_points=0.0, benign_points=0.0
        )
        self.assertIsNot(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_comparable_pathogenic_and_benign_points_conflict(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=True, pathogenic_points=4.0, benign_points=4.0
        )
        self.assertIs(outcome, InterpretationOutcome.CONFLICTING_EVIDENCE)

    def test_lopsided_points_do_not_conflict(self):
        """One side overwhelms the other -- not a genuine disagreement,
        just the ordinary case of most evidence pointing one way."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=True, pathogenic_points=8.0, benign_points=1.0
        )
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_one_sided_points_with_none_on_the_other_is_interpreted_not_conflicting(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=True, pathogenic_points=6.0, benign_points=0.0
        )
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_conflict_fires_even_when_the_gene_did_not_resolve(self):
        """A real interpretive disagreement is worth reporting on its
        own terms -- it is not gated behind gene resolution the way
        insufficient_evidence is."""
        outcome = determine_interpretation_outcome(
            gene_resolved=False, evidence_queries_completed=True, pathogenic_points=4.0, benign_points=4.0
        )
        self.assertIs(outcome, InterpretationOutcome.CONFLICTING_EVIDENCE)

    def test_review_required_is_never_returned_by_this_function(self):
        """HELD: no combination of these four inputs reaches
        review_required in this commit -- its trigger set is proposed,
        not implemented, per the dispatch's own boundary."""
        for gene_resolved in (True, False):
            for evidence_queries_completed in (True, False):
                for pathogenic_points in (0.0, 1.0, 4.0, 8.0, None):
                    for benign_points in (0.0, 1.0, 4.0, 8.0, None):
                        outcome = determine_interpretation_outcome(
                            gene_resolved=gene_resolved,
                            evidence_queries_completed=evidence_queries_completed,
                            pathogenic_points=pathogenic_points,
                            benign_points=benign_points,
                        )
                        self.assertIsNot(outcome, InterpretationOutcome.REVIEW_REQUIRED)


class TestAcmgRuleEngineNeverRelabelsAFailureAsInsufficiency(unittest.TestCase):
    """The integration-level proof: `ACMGRuleEngine.evaluate()` itself,
    not just the pure function, must not relabel a failure."""

    def test_all_clean_and_empty_with_gene_resolved_is_insufficient_evidence(self):
        """The baseline this file's other tests are contrasted against:
        genuinely no usable evidence, cleanly, IS insufficient_evidence."""
        result = _evaluate()
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_a_source_failing_on_purpose_does_not_produce_insufficient_evidence(self):
        """THE test the dispatch exists to require: make a source fail
        on purpose (a genuine `error` key, not `found: False`, not a
        skip) and prove the outcome is NOT insufficient_evidence."""
        result = _evaluate(gnomad_result={"found": False, "error": "gnomAD API timed out after 30s"})
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)

    def test_an_unresolved_gene_does_not_produce_insufficient_evidence(self):
        result = _evaluate(clingen_result={"gene_resolution_status": "not_found"})
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)

    def test_an_ambiguous_gene_does_not_produce_insufficient_evidence(self):
        result = _evaluate(
            clingen_result={"gene_resolution_status": "ambiguous", "gene_resolution_candidates": ["A", "B"]}
        )
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_a_structural_skip_is_not_a_failure_and_can_still_be_insufficient(self):
        """A `found: False`/skip (no `error` key) is not a query
        failure -- it must NOT block insufficient_evidence the way a
        real error does, or the gate would be too broad and everything
        would fall through to `interpreted`."""
        result = _evaluate(alphamissense_result={"skipped": True, "reason": "not eligible for this variant"})
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)


class TestInterpretationResultCarriesTheOutcome(unittest.TestCase):
    """The outcome must thread through `build_interpretation_result`
    (Phase 2's canonical object) exactly the way `acmg_net_points` and
    `acmg_classification` already do -- carried alongside, not
    replacing."""

    def test_interpretation_result_to_dict_exposes_interpretation_outcome(self):
        from pipeline.interpretation_result import build_interpretation_result

        acmg_result = _evaluate()
        interpretation = {"acmg_evaluation": acmg_result, "supporting_evidence": []}
        result_obj = build_interpretation_result(
            variant_dict=_VARIANT,
            interpretation=interpretation,
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={"hit_count": 0},
            clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        )
        as_dict = result_obj.to_dict()
        self.assertIn("interpretation_outcome", as_dict)
        self.assertEqual(as_dict["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        # ALONGSIDE, not replacing: the ACMG classification is still
        # present and unrelated to the outcome value.
        self.assertIn("acmg_classification", as_dict)


if __name__ == "__main__":
    unittest.main()
