"""
pipeline/interpretation_outcome.py
───────────────────────────────────

THE INTERPRETATION-OUTCOME STATE: a PER-VARIANT judgement about the
interpretation, carried in geper_results.json and in the report
ALONGSIDE the ACMG classification, NOT replacing it (human ruling,
2026-09-09/10). Four values:

  interpreted             -- the ordinary case: criteria were evaluated
                              and whatever the classification turned out
                              to be, the interpretation stands.
  insufficient_evidence   -- no criterion fired in either direction, and
                              ONLY when it is honestly true: the gene
                              resolved AND every evidence query this
                              variant attempted actually completed. See
                              `determine_interpretation_outcome`'s own
                              docstring for why this gate exists.
  conflicting_evidence    -- criteria fired in BOTH directions at
                              comparable weight: a genuine interpretive
                              disagreement (disagreeing ClinVar
                              submissions, functional data against
                              population frequency), never a data-format
                              mismatch between two sources' shapes. AS OF
                              ROUND 2 (below), this evidence pattern no
                              longer surfaces as this string -- see
                              `review_required`.
  review_required         -- RULED (2026-09-09/10), approved exactly as
                              proposed. Fires on EITHER of two triggers:
                              (1) the evidence pattern that would
                              otherwise be `conflicting_evidence`
                              (comparable-weight pathogenic/benign
                              points); (2) `acmg_classification` in
                              {"Pathogenic", "Likely Pathogenic"}. The
                              human's own constraint from round 1 ("an
                              engine-level flag that fires on all of
                              them is inert") is why `insufficient_evidence`
                              and "any query failed" were both
                              considered and REJECTED as triggers -- see
                              `determine_interpretation_outcome`'s own
                              docstring for the full argument.

THE COLLISION (round 2, mine, stated explicitly): a variant CAN satisfy
both triggers at once (pathogenic_points=12, benign_points=6 is
comparable-weight AND nets to Likely Pathogenic). Only one string can be
`interpretation_outcome`, so on EITHER trigger the returned value is
`review_required` -- meaning `conflicting_evidence`, as a RETURNED
value, is now UNREACHABLE via `determine_interpretation_outcome`: every
input that would have produced it now escalates instead. WHAT IS LOST:
a reader who sees only `review_required` cannot tell, from that field
alone, whether it fired for a genuine evidence disagreement, a P/LP
call, or both. THE ANSWER: `determine_review_required_reasons` below
computes the same two conditions independently and returns the list of
which fired -- ALWAYS present (empty when review_required did not
fire), same "checked-and-clean vs never-checked" discipline as
`pipeline/provenance.py::get_stale_fallbacks`.

THIS IS A DIFFERENT AXIS from the per-source retrieval states already in
the engine -- `pipeline/stage_schemas.py::StageStatus`
(not_run/error/not_found/found) -- and from `pipeline/clingen/utils.py::
GeneResolutionStatus` (not_found/ambiguous/resolved). A variant can have
every source in `found` and still land in review_required; it can have
half its sources absent and still interpret cleanly if the criteria
that fired never depended on them. Do NOT derive this state from those
retrieval states -- that collapse is exactly the defect this module
exists to prevent.
"""

from __future__ import annotations

import enum
from typing import List, NoReturn, Optional


class InterpretationOutcome(str, enum.Enum):
    INTERPRETED = "interpreted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    REVIEW_REQUIRED = "review_required"

    def __bool__(self) -> NoReturn:
        # Same idiom as StageStatus/VersionStatus/RetrievalMode: every
        # member of a `str` enum is truthy, so `if x:` would silently
        # collapse all four members into one answer -- exactly the
        # distinction this type exists to keep. Compare explicitly,
        # e.g. `x is InterpretationOutcome.INSUFFICIENT_EVIDENCE`.
        raise TypeError(
            "InterpretationOutcome has no truth value -- compare explicitly, e.g. "
            "`x is InterpretationOutcome.INSUFFICIENT_EVIDENCE`."
        )


# "Comparable weight" for the conflicting-evidence pattern: MY OWN
# definition (named as mine in the dispatch report), not settled by the
# ruling -- the ruling names the concept ("criteria fired in both
# directions at comparable weight") but not a number. Neither side may
# be less than half the other's magnitude; open to a different ratio if
# the human rules one.
_COMPARABLE_WEIGHT_RATIO = 0.5

# review_required trigger 2 (RULED, 2026-09-09/10, approved as
# proposed): the highest-stakes, most actionable classifications --
# worth a second look before reaching a clinician even when the engine
# itself is confident. See this module's own docstring for why
# insufficient_evidence and "any query failed" were considered and
# rejected as additional triggers.
_REVIEW_REQUIRED_CLASSIFICATIONS = frozenset({"Pathogenic", "Likely Pathogenic"})


def _is_comparable_weight_conflict(pathogenic_points: Optional[float], benign_points: Optional[float]) -> bool:
    """The evidence pattern that used to surface as
    `InterpretationOutcome.CONFLICTING_EVIDENCE` directly (round 1) and
    is now review_required trigger 1 (round 2). Factored out so
    `determine_interpretation_outcome` and `determine_review_required_reasons`
    can never disagree about WHICH inputs satisfy it -- only about how
    many strings the result collapses to."""
    resolved_pathogenic_points = pathogenic_points or 0.0
    resolved_benign_points = benign_points or 0.0
    if resolved_pathogenic_points <= 0 or resolved_benign_points <= 0:
        return False
    larger = max(resolved_pathogenic_points, resolved_benign_points)
    smaller = min(resolved_pathogenic_points, resolved_benign_points)
    return smaller >= larger * _COMPARABLE_WEIGHT_RATIO


def _base_outcome(
    *,
    gene_resolved: bool,
    evidence_queries_completed: bool,
    pathogenic_points: Optional[float],
    benign_points: Optional[float],
) -> InterpretationOutcome:
    """
    The three-way determination from before round 2 (conflicting /
    insufficient / interpreted), UNCHANGED in its own logic -- see
    `determine_interpretation_outcome`'s docstring for the reasoning
    behind each branch. Private: round 2's escalation layer
    (`determine_interpretation_outcome`) is the only public entry point
    that returns an outcome derived from evidence; this helper's
    CONFLICTING_EVIDENCE return value is an intermediate the escalation
    layer consumes, not something a caller outside this module should
    read as a final answer.
    """
    resolved_pathogenic_points = pathogenic_points or 0.0
    resolved_benign_points = benign_points or 0.0

    if _is_comparable_weight_conflict(pathogenic_points, benign_points):
        return InterpretationOutcome.CONFLICTING_EVIDENCE

    if gene_resolved and evidence_queries_completed and resolved_pathogenic_points == 0 and resolved_benign_points == 0:
        return InterpretationOutcome.INSUFFICIENT_EVIDENCE

    return InterpretationOutcome.INTERPRETED


def determine_interpretation_outcome(
    *,
    gene_resolved: bool,
    evidence_queries_completed: bool,
    pathogenic_points: Optional[float],
    benign_points: Optional[float],
    classification: Optional[str],
) -> InterpretationOutcome:
    """
    Pure function. No evidence is fabricated and no classification is
    forced -- it reads only the five inputs named above, never a raw
    evidence dict itself. Callers derive `gene_resolved` from
    `GeneResolutionStatus` (`is GeneResolutionStatus.RESOLVED`, not a
    truthy check), `evidence_queries_completed` from whether any
    evidence-query result this variant actually attempted came back
    with a genuine `error` key set (never a `found: False` or a
    structural skip, e.g. the mtDNA compartment gate's pre-built skip
    results, neither of which is a failure), and `classification` from
    `ACMGRuleEngine._combine`'s own `CombineResult.classification`.

    STEP 1 -- the base evidence pattern (`_base_outcome`, private,
    unchanged since round 1): conflicting / insufficient / interpreted.
    insufficient_evidence requires ALL of: no criterion fired in either
    direction, the gene resolved, AND every evidence query completed.
    THE REASON THIS GATE EXISTS (human ruling, verbatim): "if failures
    can land in the insufficiency bucket, the insufficiency rate stops
    measuring the genomics and starts measuring a mixture of that and
    whatever is broken upstream; a source failing silently for a week
    would be invisible." So an unresolved gene, a query failure, a
    parsing failure, or an infrastructure error -- ANY of them -- fails
    this gate, and the base outcome falls through to `interpreted`
    instead: not because an interpretation was actually reached, but
    because mislabeling it `insufficient_evidence` would be worse -- it
    would corrupt a rate a reader trusts to mean something about the
    biology. The real failure remains visible elsewhere (the variant's
    own `errors` list, `clingen.gene_resolution_status`), never erased
    -- only kept out of THIS specific bucket. THIS SAME REASONING IS WHY
    "any query failed" was REJECTED as a review_required trigger below,
    round 2 (human ruling, verbatim): "it puts an OPERATIONAL condition
    into a bucket reviewers read as an INTERPRETIVE judgement ... during
    an outage it spikes toward 100% and the flag stops meaning THIS
    VARIANT NEEDS A CLINICIAN and starts meaning THE PIPELINE IS
    UNWELL." Query failures surface through the failure states
    (`interpreted`, honestly, plus the variant's own `errors` list), not
    by escalating a variant no clinician can actually help with.

    STEP 2 -- the review_required escalation (round 2, RULED, approved
    exactly as proposed): if the base outcome is CONFLICTING_EVIDENCE,
    OR `classification` is Pathogenic/Likely Pathogenic, the returned
    value is REVIEW_REQUIRED instead of the base outcome. `insufficient_evidence`
    was considered and REJECTED as a third trigger (human ruling,
    verbatim): "on a sparse panel it approaches blanket firing, and a
    flag that fires on most variants tells a reviewer nothing about
    which to look at first."

    THE COLLISION (mine, stated explicitly -- see this module's own
    docstring for the full argument): a variant can satisfy BOTH
    triggers simultaneously. Only one string is returned either way --
    REVIEW_REQUIRED -- because an escalation takes priority over naming
    which evidence pattern produced it. Call
    `determine_review_required_reasons` with the SAME inputs if you need
    to know which trigger(s) actually fired; that information is never
    silently erased, only moved to a different, always-present field.

    A CONSEQUENCE WORTH STATING PLAINLY: `InterpretationOutcome.CONFLICTING_EVIDENCE`
    is now UNREACHABLE as this function's return value -- every input
    that would produce it is, by construction, also an input that
    triggers the escalation above. The enum member still exists (for
    `_base_outcome`'s and `determine_review_required_reasons`'s own use).
    """
    base = _base_outcome(
        gene_resolved=gene_resolved,
        evidence_queries_completed=evidence_queries_completed,
        pathogenic_points=pathogenic_points,
        benign_points=benign_points,
    )
    is_pathogenic_or_likely_pathogenic = classification in _REVIEW_REQUIRED_CLASSIFICATIONS
    if base is InterpretationOutcome.CONFLICTING_EVIDENCE or is_pathogenic_or_likely_pathogenic:
        return InterpretationOutcome.REVIEW_REQUIRED
    return base


def determine_review_required_reasons(
    *,
    pathogenic_points: Optional[float],
    benign_points: Optional[float],
    classification: Optional[str],
) -> List[str]:
    """
    THE ANSWER TO "if both need to be visible, say so" (round 2,
    dispatch's own instruction on the collision). Recomputes the SAME
    two trigger conditions `determine_interpretation_outcome` uses (via
    the same private `_is_comparable_weight_conflict` helper, so the two
    functions can never disagree about which conditions fired) and
    returns every one that did, in a stable order: conflicting-evidence
    first, then classification. ALWAYS a list -- empty, never omitted,
    when `review_required` did not fire -- so a reader can tell "checked,
    not escalated" from "never checked", the same discipline as
    `pipeline/provenance.py::get_stale_fallbacks`.

    Deliberately does NOT take `gene_resolved`/`evidence_queries_completed`:
    neither review_required trigger is gated on them (see this module's
    docstring on why insufficient_evidence-as-a-trigger and "any query
    failed" were both rejected), so there is nothing for this function
    to read there.
    """
    reasons: List[str] = []
    if _is_comparable_weight_conflict(pathogenic_points, benign_points):
        reasons.append("conflicting_evidence")
    if classification in _REVIEW_REQUIRED_CLASSIFICATIONS:
        reasons.append("classification_pathogenic_or_likely_pathogenic")
    return reasons
