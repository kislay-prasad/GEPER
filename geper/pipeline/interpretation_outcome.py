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
                              mismatch between two sources' shapes.
  review_required         -- HELD. The member exists so downstream code
                              has a name to reference, but this commit
                              implements no trigger set for it -- the
                              human's own constraint ("every variant
                              requires human review already, so an
                              engine-level flag that fires on all of
                              them is inert") is not yet ruled on for
                              this axis. `determine_interpretation_outcome`
                              below never returns it.

THIS IS A DIFFERENT AXIS from the per-source retrieval states already in
the engine -- `pipeline/stage_schemas.py::StageStatus`
(not_run/error/not_found/found) -- and from `pipeline/clingen/utils.py::
GeneResolutionStatus` (not_found/ambiguous/resolved). A variant can have
every source in `found` and still land in conflicting_evidence; it can
have half its sources absent and still interpret cleanly if the criteria
that fired never depended on them. Do NOT derive this state from those
retrieval states -- that collapse is exactly the defect this module
exists to prevent.
"""

from __future__ import annotations

import enum
from typing import NoReturn, Optional


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


# "Comparable weight" for conflicting_evidence: MY OWN definition (named
# as mine in the dispatch report), not settled by the ruling -- the
# ruling names the concept ("criteria fired in both directions at
# comparable weight") but not a number. Neither side may be less than
# half the other's magnitude; open to a different ratio if the human
# rules one.
_COMPARABLE_WEIGHT_RATIO = 0.5


def determine_interpretation_outcome(
    *,
    gene_resolved: bool,
    evidence_queries_completed: bool,
    pathogenic_points: Optional[float],
    benign_points: Optional[float],
) -> InterpretationOutcome:
    """
    Pure function. No evidence is fabricated and no classification is
    forced -- it reads only the four inputs named above, never a raw
    evidence dict itself. Callers derive `gene_resolved` from
    `GeneResolutionStatus` (`is GeneResolutionStatus.RESOLVED`, not a
    truthy check) and `evidence_queries_completed` from whether any
    evidence-query result this variant actually attempted came back
    with a genuine `error` key set -- never from a `found: False` or a
    structural skip (e.g. the mtDNA compartment gate's pre-built skip
    results), neither of which is a failure.

    conflicting_evidence is checked FIRST, independently of the other
    two conditions: a real interpretive disagreement is worth reporting
    even when the gene did not cleanly resolve or a query failed
    elsewhere -- those are two different problems, and burying a
    genuine conflict under an unrelated infrastructure gap would be its
    own honesty failure.

    insufficient_evidence requires ALL of: no criterion fired in either
    direction, the gene resolved, AND every evidence query completed.
    THE REASON THIS GATE EXISTS (human ruling, verbatim): "if failures
    can land in the insufficiency bucket, the insufficiency rate stops
    measuring the genomics and starts measuring a mixture of that and
    whatever is broken upstream; a source failing silently for a week
    would be invisible." So an unresolved gene, a query failure, a
    parsing failure, or an infrastructure error -- ANY of them -- fails
    this gate, and the outcome falls through to `interpreted` instead:
    not because an interpretation was actually reached, but because
    mislabeling it `insufficient_evidence` would be worse -- it would
    corrupt a rate a reader trusts to mean something about the biology.
    The real failure remains visible elsewhere (the variant's own
    `errors` list, `clingen.gene_resolution_status`), never erased --
    only kept out of THIS specific bucket.

    `review_required` is never returned here -- see this module's own
    docstring.
    """
    resolved_pathogenic_points = pathogenic_points or 0.0
    resolved_benign_points = benign_points or 0.0

    if resolved_pathogenic_points > 0 and resolved_benign_points > 0:
        larger = max(resolved_pathogenic_points, resolved_benign_points)
        smaller = min(resolved_pathogenic_points, resolved_benign_points)
        if smaller >= larger * _COMPARABLE_WEIGHT_RATIO:
            return InterpretationOutcome.CONFLICTING_EVIDENCE

    if gene_resolved and evidence_queries_completed and resolved_pathogenic_points == 0 and resolved_benign_points == 0:
        return InterpretationOutcome.INSUFFICIENT_EVIDENCE

    return InterpretationOutcome.INTERPRETED
