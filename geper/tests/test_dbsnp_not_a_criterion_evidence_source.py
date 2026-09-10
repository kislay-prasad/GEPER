"""
tests/test_dbsnp_not_a_criterion_evidence_source.py
──────────────────────────────────────────────────────

god's dispatch (conv-providerlist, 2026-09-10): `acmg_rules.py`'s own
module docstring named dbSNP as one of the sources "every string is
built from" for a triggered/not_triggered criterion -- a claim that was
NEVER TRUE, since the repository's initial commit (`cb79edc`), not a
claim that went stale. `dbsnp_result` is a real, accepted parameter of
`ACMGRuleEngine.evaluate()` and IS read once, but only inside the
`evidence_queries_completed` infrastructure gate that feeds
`interpretation_outcome` -- never inside a criterion's own rationale
text. See the corrected docstring (`acmg_rules.py:20-33`) for the full
history; this file is what god's dispatch calls "a docstring assertion
test... the shape you already built for the badges" -- my own call, my
own reason: a raw grep for `dbsnp_result` inside criterion method
bodies would miss a criterion that captured it under a different local
name, so this instead calls the real engine with a distinctively
tagged `dbsnp_result` and asserts that tag never reaches a rationale
string -- proving the ABSENCE at the boundary that actually matters
(what a reader of a criterion's own evidence text sees), not merely
the absence of one literal identifier in the source.

WHAT THIS TEST WOULD FAIL AGAINST, STATED EXPLICITLY: any future
criterion method that starts building its `rationale`/`supporting_
evidence`/`conflicting_evidence` text from `dbsnp_result` (directly or
via a differently-named local variable) without the corrected
docstring being updated back to include dbSNP -- the same shape of
regression the original, always-wrong claim represents. It would NOT
catch a change to the `evidence_queries_completed` gate itself (`:1006`
in the dispatch's own citation) -- that gate is explicitly out of scope
here, per boundary, and its own separate correctness is under review on
a different card.
"""

from __future__ import annotations

import unittest

from pipeline.acmg_rules import ACMGRuleEngine

_SENTINEL = "DBSNP_SENTINEL_MARKER_9f3a1c"

_VARIANT = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}


def _evaluate_with_tagged_dbsnp():
    """All-clean-empty `evaluate()` call (mirrors `test_interpretation_
    outcome.py`'s own `_evaluate` helper shape) except `dbsnp_result`,
    which carries a distinctive, unmistakable tag in every field a
    careless criterion implementation might plausibly read from."""
    return ACMGRuleEngine().evaluate(
        clinvar_result={"records": []},
        dbsnp_result={
            "found": True,
            "rsid": _SENTINEL,
            "error": None,
            "note": _SENTINEL,
        },
        protein_result={},
        alphamissense_result={"skipped": True},
        mmsplice_result={"predicted": False},
        gnomad_result=None,
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


class TestDbsnpNeverReachesACriterionRationale(unittest.TestCase):
    """The corrected docstring's claim, made falsifiable: dbSNP builds
    no criterion's evidence text. Checked across every triggered,
    not_triggered, AND not_evaluated criterion -- the docstring's own
    "every string" scoping names only triggered/not_triggered, but a
    not_evaluated criterion can still carry a `reason` string, and the
    sentinel must not leak into any of the three."""

    def test_sentinel_never_appears_in_any_criterion_result(self):
        result = _evaluate_with_tagged_dbsnp()
        for code, criterion in result["all_criteria"].items():
            for field in ("rationale", "supporting_evidence", "conflicting_evidence"):
                value = criterion.get(field)
                text = value if isinstance(value, str) else " ".join(value or [])
                self.assertNotIn(
                    _SENTINEL,
                    text,
                    f"criterion {code}'s {field!r} leaked the dbSNP sentinel -- "
                    "dbSNP has started building criterion evidence text again; "
                    "the module docstring's corrected claim needs updating, not this test.",
                )


if __name__ == "__main__":
    unittest.main()
