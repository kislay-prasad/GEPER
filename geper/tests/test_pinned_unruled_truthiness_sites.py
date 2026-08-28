"""
PINNED, NOT ENDORSED. Three truthiness sites that are still UNRULED.

Every test in this file asserts WHAT HAPPENS TODAY. Not one of them
asserts that today's behaviour is correct, and none of them should be
read as agreeing with it. They exist so that whoever rules on these
three questions finds the current answer WRITTEN DOWN rather than
having to re-derive it from the code and then guess whether it was
deliberate.

WHY PIN INSTEAD OF FIX. These three differ in kind from the 18 sites
already fixed (12 stage guards in 85c651c, 6 provenance guards
alongside this file). Those were RECORDS -- a failed lookup logged as a
success. These three change CLINICAL OUTPUT or the run's own account of
it, so "fix the truthiness bug" is not a mechanical call: each one
first needs a count of how many variants currently produce output that
would change. The counts are done and reported; the rulings are not.

WHAT AN UNPINNED CURRENT BEHAVIOUR ACTUALLY IS. Not neutral, and not
"no decision yet": it is an undecided answer already in force on every
run, quietly, with nobody having chosen it. Writing it into a test does
not endorse it -- it makes it visible and makes any change to it
deliberate, which is the whole difference between a decision and a
drift.

--------------------------------------------------------------------
SITE 1  PP4 fires on an EMPTY curated disease list
        pipeline/acmg_rules.py, the `distinct_diseases` half of _pp4
        Open question: is "this gene has no curated diseases at all" a
        SINGLE GENETIC ETIOLOGY? 58f5fe5 fixed only the UNKNOWN case
        (missing key / None). `[]` is not None, so it survives that
        guard untouched and still satisfies the condition.

SITE 2  orchestrator.py:1313  _classify_variant_result
        `if context.get("error"):` -- an empty-string sequence-context
        error does not mark the variant "skipped", so it is counted and
        reported as analysed.

SITE 3  orchestrator.py:1786  conservation provenance
        `error = conservation_result.get("error")` then `if error and
        not found:` -- an ASSIGNMENT, which is why no AST scan keyed on
        `ast.If` tests ever saw it. An empty-string conservation error
        records no failure.
--------------------------------------------------------------------
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.hpo.models import HPOGeneEvidence, HPOPhenotypeAssociation
from pipeline.hpo.utils import build_phenotype_result
from pipeline.orchestrator import GeperPipeline
from pipeline.provenance import VersionStatus


# ---------------------------------------------------------------------
# SITE 1 -- PP4 and the empty disease list
# ---------------------------------------------------------------------

MARFAN_TERMS = "HP:0001166,HP:0000098"


def _fbn1_evidence(disease_id="OMIM:154700"):
    """Real `HPOGeneEvidence.to_dict()` output, not a hand-typed dict."""
    associations = [
        HPOPhenotypeAssociation(
            gene_symbol="FBN1", hpo_id="HP:0001166", hpo_name="Arachnodactyly", disease_id=disease_id
        ),
        HPOPhenotypeAssociation(
            gene_symbol="FBN1", hpo_id="HP:0000098", hpo_name="Tall stature", disease_id=disease_id
        ),
    ]
    return HPOGeneEvidence(
        gene_symbol="FBN1",
        source="local_dataset",
        found=True,
        phenotype_associations=associations,
        ncbi_gene_id="2200",
    ).to_dict()


def _pp4(hpo_result, terms=MARFAN_TERMS):
    result = ACMGRuleEngine().evaluate(phenotype_result=build_phenotype_result(terms, None), hpo_result=hpo_result)
    return result["all_criteria"]["PP4"]


class TestSite1_PP4OnAnEmptyDiseaseList(unittest.TestCase):
    """PINNED, NOT ENDORSED.

    OPEN QUESTION: should a gene with zero curated disease entries
    satisfy PP4's "a disease with a single genetic etiology"? The
    reviewing position is that it should not -- a gene with no curated
    diseases has not answered the question either way -- but that is a
    lean, not a ruling, and the fix would disable PP4 for every gene
    with an empty list.

    COUNT AS REPORTED: 0 of the 5,268 gene symbols in the real HPO
    `genes_to_phenotype.txt` release produce an empty list, so no
    variant routed through the LOCAL dataset can reach this. The live
    API fallback CAN reach it (`provider.py:273` builds phenotype
    associations with no disease_id at all, so `distinct_disease_ids`
    comes only from a separate `diseases` payload that may be empty)
    and was not counted -- it needs live network calls.
    """

    def test_an_empty_disease_list_currently_triggers_pp4(self):
        evidence = _fbn1_evidence()
        evidence["distinct_disease_ids"] = []
        pp4 = _pp4(evidence)
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["details"]["distinct_disease_count"], 0)

    def test_the_landed_none_guard_does_not_cover_the_empty_list(self):
        """The two states 58f5fe5 separated, asserted side by side.

        This is the assertion that keeps the empty-list case from being
        mistaken for something already handled: `None` is now
        not_evaluated, `[]` still triggers, and the ONLY difference is
        which falsy value the key holds.
        """
        unknown = _fbn1_evidence()
        unknown["distinct_disease_ids"] = None
        empty = _fbn1_evidence()
        empty["distinct_disease_ids"] = []
        self.assertEqual(_pp4(unknown)["status"], "not_evaluated")
        self.assertEqual(_pp4(empty)["status"], "triggered")

    def test_a_curated_single_disease_gene_is_the_case_nobody_disputes(self):
        """Control. Whatever the ruling, this must keep triggering."""
        pp4 = _pp4(_fbn1_evidence())
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["details"]["distinct_disease_count"], 1)


# ---------------------------------------------------------------------
# SITE 2 -- orchestrator.py:1313
# ---------------------------------------------------------------------


def _classify(variant_result):
    """`_classify_variant_result` is a @staticmethod, so it is called
    off the class with no instance -- no double needed at all."""
    return GeperPipeline._classify_variant_result(variant_result)


_OK_INTERPRETATION = {"classification": "Likely pathogenic"}


class TestSite2_ClassifyVariantResultOnAnEmptyContextError(unittest.TestCase):
    """PINNED, NOT ENDORSED.

    OPEN QUESTION: `if context.get("error"):` decides whether a variant
    is reported as "skipped". An empty-string error means the sequence
    context failed and said nothing about why; today that variant is
    counted as analysed.

    COUNT AS REPORTED: 0 variants. `sequence_context["error"]` has
    exactly ONE producer in the tree -- orchestrator.py:1549, the
    hard-coded literal "sequence context unavailable" -- and
    pipeline/sequence_context.py emits no "error" key at all. The key is
    therefore always either absent (`.get` -> None, both forms False) or
    that non-empty constant (both forms True). The defect is real and
    the value space is currently closed against it.
    """

    def test_an_empty_context_error_is_not_currently_skipped(self):
        self.assertEqual(
            _classify({"sequence_context": {"error": ""}, "interpretation_result": _OK_INTERPRETATION}),
            "success",
        )

    def test_the_only_error_value_production_can_emit_is_skipped(self):
        """The literal from orchestrator.py:1549, verbatim. If someone
        changes that string this test still passes; if someone makes it
        emptyable, the test above is the one that documents what then
        happens."""
        self.assertEqual(
            _classify(
                {
                    "sequence_context": {"error": "sequence context unavailable"},
                    "interpretation_result": _OK_INTERPRETATION,
                }
            ),
            "skipped",
        )

    def test_the_other_classifications_are_unaffected(self):
        """Controls -- the three outcomes that must not move whichever
        way the empty-string case is ruled."""
        self.assertEqual(_classify({"out_of_scope": True}), "out_of_scope")
        self.assertEqual(_classify({"sequence_context": {}, "interpretation_result": {"error": "x"}}), "failed")
        self.assertEqual(_classify({"sequence_context": {}}), "failed")
        self.assertEqual(_classify({"sequence_context": {}, "interpretation_result": _OK_INTERPRETATION}), "success")

    def test_a_missing_error_key_is_indistinguishable_from_a_none_one(self):
        """Pinned because it is the half a fix must NOT change: absent
        and None both mean "no error", and `is not None` keeps them that
        way."""
        for context in ({}, {"error": None}):
            with self.subTest(context=context):
                self.assertEqual(
                    _classify({"sequence_context": context, "interpretation_result": _OK_INTERPRETATION}),
                    "success",
                )


# ---------------------------------------------------------------------
# SITE 3 -- orchestrator.py:1786
# ---------------------------------------------------------------------


class _RecordingProvenance:
    def __init__(self):
        self.calls = []

    def record(self, source, status, **kwargs):
        self.calls.append((source, status, kwargs))

    def notes_for(self, source):
        return [kw.get("notes") or "" for src, _s, kw in self.calls if src == source]


class _FakeSequenceContextGen:
    assembly = "GRCh38"


UCSC = "Conservation (PhyloP/PhastCons, UCSC)"


def _capture_conservation(conservation_result):
    pipeline = object.__new__(GeperPipeline)
    pipeline.provenance = _RecordingProvenance()
    pipeline.sequence_context_gen = _FakeSequenceContextGen()
    pipeline._capture_stage_provenance(
        clinvar_result={},
        dbsnp_result={},
        gnomad_result=None,
        uniprot_result=None,
        interpro_result=None,
        alphafold_result=None,
        functional_evidence_result=None,
        conservation_result=conservation_result,
    )
    return pipeline.provenance


class TestSite3_ConservationErrorAssignment(unittest.TestCase):
    """PINNED, NOT ENDORSED.

    OPEN QUESTION: `error = conservation_result.get("error")` followed
    by `if error and not conservation_result.get("found"):`. An
    empty-string error records no failure against the conservation
    sources. This is the site that is an ASSIGNMENT rather than an `if`
    test, which is exactly why the AST sweep that found the other
    sites missed it -- the sweep was keyed on `ast.If` tests, and a
    scan is bounded by the shape it searched for.

    COUNT AS REPORTED: 0 variants. Every value this key can hold is
    enumerated: `ConservationAnnotation.from_error` is reached from 3
    call sites in pipeline/conservation/provider.py -- two `str(exc)`
    on an `ExternalAPIError` (:246, :362) and one
    `f"{provider.name} raised: {exc}"` (:478, never empty). Both
    `_get`s that can raise it do so with a non-empty f-string literal
    and use `requests` directly, so no other raise reaches here. The
    fourth path, `str(payload["error"])` at :252, sits behind its own
    `if payload.get("error"):` -- an empty error body is filtered by
    the same truthiness bug one line above, so it cannot arrive empty.
    """

    def test_an_empty_conservation_error_currently_records_no_failure(self):
        provenance = _capture_conservation({"error": "", "found": False, "source": "ucsc_api"})
        self.assertFalse(
            any("Most recent query failed" in note for note in provenance.notes_for(UCSC)),
            provenance.calls,
        )

    def test_a_real_conservation_error_does_record_a_failure(self):
        """The half that already works, pinned so a fix here is judged
        against both directions."""
        provenance = _capture_conservation({"error": "HTTP 500", "found": False, "source": "ucsc_api"})
        self.assertTrue(
            any("Most recent query failed" in note for note in provenance.notes_for(UCSC)),
            provenance.calls,
        )

    def test_a_successful_query_still_records_only_a_timestamp(self):
        """Control. `found` true means scores arrived; no failure should
        ever be recorded for it, whichever way the empty case is ruled."""
        provenance = _capture_conservation({"error": "", "found": True, "source": "ucsc_api"})
        statuses = [status for src, status, _kw in provenance.calls if src == UCSC]
        self.assertIn(VersionStatus.TIMESTAMP_ONLY, statuses)
        self.assertNotIn(VersionStatus.UNKNOWN, statuses)

    def test_a_skipped_conservation_result_records_nothing(self):
        provenance = _capture_conservation({"skipped": True, "error": ""})
        self.assertEqual(provenance.notes_for(UCSC), [])


if __name__ == "__main__":
    unittest.main()
