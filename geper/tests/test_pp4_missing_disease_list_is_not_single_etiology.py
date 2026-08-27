"""
Card: pp4-treats-a-missing-disease-list-as-a-satisfied-single-etiology.

PP4 requires BOTH halves of "the patient's phenotype is highly specific
for a disease with a single genetic etiology":

    specific_enough = overlap_ratio >= cfg.PP4_OVERLAP_THRESHOLD
    single_etiology = len(distinct_diseases) <= cfg.PP4_MAX_DISTINCT_DISEASES
    if specific_enough and single_etiology:   -> PP4 triggered

and built the second half's input like this:

    distinct_diseases = hpo_result.get("distinct_disease_ids") or []

A MISSING key becomes `[]`, and `len([]) <= N` is True, so "we do not
know how many diseases this gene is curated against" was resolved into
the affirmative condition PP4 needs. The direction of failure is toward
FIRING a pathogenic-supporting rule -- the opposite of the convention
this codebase states in its own words at
`kim_pipeline/pipeline/orchestration/shared.py`: "absence must not read
as a confirmed favorable result."

WHAT KEPT THIS FROM BEING A LIVE BUG, AND WHY THAT IS NOT A DEFENCE

Every producer that reaches `_pp4` with `found=True` goes through
`HPOGeneEvidence.to_dict()` (hpo/models.py:112), which always emits the
key; the shapes that omit it -- `HPOLookup.query_variant`'s early
returns and `_run_hpo_stage`'s exception path -- all carry `found=False`
and are filtered by the guard above. So the affirmative branch cannot be
reached from today's callers, and this file's dangerous cases build the
shape by hand.

That makes this latent, not live, and it is graded on the direction of
failure rather than on today's reachability: the safety of an
affirmative clinical claim rests entirely on a sibling module continuing
to emit a key it is under no contract to emit. Correctness that depends
on a distant collaborator's current habit is a coincidence, not a guard.

The same line one above is the counter-example that makes the point:

    gene_terms = {... for t in (hpo_result.get("distinct_phenotype_terms") or [])}

identical shape, and it fails SAFE -- an empty term set drives
overlap_ratio to 0, `specific_enough` is False, and PP4 cannot fire. Two
lines, one apart, same idiom, opposite failure directions, and nothing
at the call site tells you which is which.

SCOPE -- WHAT THIS FILE DELIBERATELY DOES NOT CHANGE

`distinct_disease_ids: []` PRESENT is a measurement: HPO was consulted
and this gene has no curated disease entries. Whether a gene with zero
curated diseases should satisfy "a single genetic etiology" is a
SEMANTIC question about the rule, not a truthiness bug, and answering it
would change the classification of real variants that get output today.
Current behaviour for the empty list is therefore PINNED BELOW UNCHANGED
and flagged for a separate ruling. Only the unknown case moves.
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.hpo.models import HPOGeneEvidence, HPOPhenotypeAssociation
from pipeline.hpo.utils import build_phenotype_result


MARFAN_TERMS = "HP:0001166,HP:0000098"


def _fbn1_evidence(disease_ids=("OMIM:154700",)):
    """Real `HPOGeneEvidence.to_dict()` output, not a hand-typed dict.

    Built through the actual model so the fixture cannot drift from the
    shape production emits -- if `to_dict()` ever stops emitting
    `distinct_disease_ids`, these tests describe what happens next rather
    than continuing to test a shape nobody produces.
    """
    associations = [
        HPOPhenotypeAssociation(
            gene_symbol="FBN1",
            hpo_id="HP:0001166",
            hpo_name="Arachnodactyly",
            disease_id=disease_ids[0] if disease_ids else None,
        ),
        HPOPhenotypeAssociation(
            gene_symbol="FBN1",
            hpo_id="HP:0000098",
            hpo_name="Tall stature",
            disease_id=disease_ids[0] if disease_ids else None,
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
    result = ACMGRuleEngine().evaluate(
        phenotype_result=build_phenotype_result(terms, None),
        hpo_result=hpo_result,
    )
    return result["all_criteria"]["PP4"]


class TestAnUnknownDiseaseListDoesNotSatisfySingleEtiology(unittest.TestCase):
    def test_a_missing_key_does_not_trigger_pp4(self):
        """THE DANGEROUS CASE. The phenotype overlap is perfect, so
        `specific_enough` is True and the whole rule turns on the second
        half -- which is being answered from data that is not there."""
        evidence = _fbn1_evidence()
        del evidence["distinct_disease_ids"]
        pp4 = _pp4(evidence)
        self.assertNotEqual(
            pp4["status"],
            "triggered",
            f"PP4 fired on a single-etiology claim derived from a disease list that was never supplied; got {pp4!r}",
        )
        self.assertEqual(
            pp4["status"],
            "not_evaluated",
            "an unknown disease count is Unknown / Insufficient Data -- not a satisfied "
            "condition, and not a failed one either",
        )

    def test_an_explicit_none_is_treated_the_same_as_a_missing_key(self):
        """`None` and absent are the same state here -- nobody told us --
        and a fix that handled only one of them would leave the other."""
        evidence = _fbn1_evidence()
        evidence["distinct_disease_ids"] = None
        self.assertEqual(_pp4(evidence)["status"], "not_evaluated")

    def test_the_rationale_names_what_was_missing(self):
        """A not_evaluated with a vague reason sends a reader looking in
        the wrong place; PP4's other not_evaluated branches all say which
        input was absent."""
        evidence = _fbn1_evidence()
        del evidence["distinct_disease_ids"]
        pp4 = _pp4(evidence)
        self.assertEqual(pp4["status"], "not_evaluated")
        rationale = pp4["rationale"].lower()
        self.assertIn("no curated disease list", rationale)
        self.assertIn("fbn1", rationale)


class TestTheHalvesThatMustKeepWorking(unittest.TestCase):
    """Controls. A fix that returned not_evaluated whenever the disease
    list was anything other than a long list would pass the class above
    and silently disable PP4."""

    def test_a_curated_single_disease_gene_still_triggers(self):
        pp4 = _pp4(_fbn1_evidence())
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["strength"], "supporting")
        self.assertEqual(pp4["details"]["distinct_disease_count"], 1)

    def test_non_matching_phenotype_terms_still_do_not_trigger(self):
        pp4 = _pp4(_fbn1_evidence(), terms="HP:9999999,HP:8888888")
        self.assertEqual(pp4["status"], "not_triggered")
        self.assertEqual(pp4["details"]["overlap_count"], 0)

    def test_too_many_distinct_diseases_still_fails_single_etiology(self):
        evidence = _fbn1_evidence()
        evidence["distinct_disease_ids"] = [f"OMIM:{n}" for n in range(100000, 100020)]
        pp4 = _pp4(evidence)
        self.assertEqual(pp4["status"], "not_triggered")
        self.assertIn("distinct HPO-curated disease entries", pp4["rationale"])

    def test_an_empty_list_that_is_actually_present_keeps_its_current_meaning(self):
        """PINNED, NOT ENDORSED, AND OUT OF SCOPE ON PURPOSE.

        `[]` present is a measurement -- HPO was consulted and this gene
        has no curated disease entries -- and today that satisfies
        `single_etiology`. Whether "no curated diseases" should count as
        "a single genetic etiology" is a question about the RULE, and
        changing it would alter the classification of variants that
        produce output today.

        This test exists so that the fix above cannot quietly change it
        as a side effect, and so the current answer is written down where
        whoever rules on it will find it.
        """
        evidence = _fbn1_evidence()
        evidence["distinct_disease_ids"] = []
        pp4 = _pp4(evidence)
        self.assertEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["details"]["distinct_disease_count"], 0)

    def test_the_sibling_line_still_fails_safe(self):
        """`distinct_phenotype_terms` uses the identical `or []` idiom one
        line above and fails SAFE -- no terms, no overlap, no PP4. It is
        pinned here because the asymmetry between the two lines is the
        thing a future reader is most likely to 'tidy' into consistency."""
        evidence = _fbn1_evidence()
        evidence["distinct_phenotype_terms"] = []
        pp4 = _pp4(evidence)
        self.assertNotEqual(pp4["status"], "triggered")
        self.assertEqual(pp4["details"]["overlap_count"], 0)


if __name__ == "__main__":
    unittest.main()
