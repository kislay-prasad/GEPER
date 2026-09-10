"""
Which providers exist, and WHICH ROLE each one plays -- as pure data.

*** READ THIS BEFORE USING THIS FILE TO ANSWER A QUESTION. ***

WHAT THIS CANNOT DO, STATED FIRST BECAUSE IT IS THE THING YOU WILL WANT.
This table makes the SET of providers enumerable. IT LEAVES CONSUMPTION
UNENUMERABLE, AND CONSUMPTION IS WHERE BOTH DIVERGENCE CLASSES LIVE -- a
provider nothing reads, and a criterion naming a source it no longer reads.
Whether a criterion actually READS a result is a property of method bodies in
`acmg_rules.py`, not of any container over providers.

The proof is `dbsnp`. It is constructed, it is called, it is an accepted
parameter of `evaluate()`, and NO CRITERION METHOD READS IT -- its only read is
the `_evidence_query_results` error gate. That was advertised as an evidence
source in `acmg_rules.py`'s module header from the repository's initial commit
until 2026-09-10. *** NO CONTAINER OVER PROVIDERS COULD HAVE CAUGHT IT, AND
THIS ONE WOULD NOT HAVE EITHER. *** It was found by reading method bodies.
So: if your question is "what does the engine read", THIS FILE CANNOT ANSWER
IT. It can tell you what is DECLARED to the engine. Those are different
questions, and the gap between them is exactly where the defects have been.

WHY ROLES AND NOT A LIST. "Provider" does not pick out one set in this
codebase; it picks out four, and they agree on only six members out of a union
of twenty-three. A flat list would have to choose one silently -- and an
enumerable list LOOKS authoritative, so the next person would test against it
instead of reading the constructor. That would be a HARDER defect to catch
than today's absence, because today nobody trusts the list: there isn't one.
Every count below is therefore a DERIVED VIEW over one table, not a rival
claim.

*** DISCARDED READINGS OF "PROVIDER", WRITTEN DOWN SO THE CHOSEN ROLES DO NOT
READ AS OBVIOUS TO THE NEXT PERSON. *** Each was defensible; each is wrong
alone:

  (a) "things named `self.*_client` in `__init__`" -> 16.
      INCLUDES: blast, alphafold, orphanet, indigenomes, thousand_genomes_sas
      -- none of which any criterion reads.
      EXCLUDES: protein, ensemble, phenotype, and every model result
      (alphamissense, mmsplice, spliceformer, splicebert).
      WHY DISCARDED: `_client` is a NAMING CONVENTION THAT DISAGREES WITH
      ITSELF -- 13 of those 16 are `*Lookup` classes and only 3 are `*Client`.
      It also drops `protein_translator`, whose result PVS1 reads, purely
      because of what it is called. A count from this reading was circulated
      as a fact about the system; it is a fact about a regular expression.

  (b) "things supplying a `*_result` that a criterion actually reads" -> 17.
      INCLUDES: protein, ensemble, phenotype and the model results.
      EXCLUDES: blast, alphafold, orphanet, indigenomes, thousand_genomes_sas
      (correctly scoped non-ACMG inputs), AND dbsnp.
      WHY DISCARDED AS THE SOLE READING: it silently deletes five providers
      that genuinely exist and do real work for the report and provenance
      surfaces. It is the right answer to "what feeds classification" and the
      wrong answer to "what must be deployed".

  (c) "what `evaluate()` declares" -> 18.
      Differs from (b) by exactly one member, `dbsnp`, and the difference is
      the whole dbSNP finding. Kept as a SEPARATE role from (b) for that
      reason: collapsing them would erase the only evidence of the defect.

  (d) "what `RawEvidenceBundle` requires" -> 11.
      *** THIS IS A ROLE IN THE TABLE BELOW, NOT A FOURTH THING BESIDE IT. ***
      It requires `blast`, `alphafold` AND `dbsnp`, NONE OF WHICH ANY
      CRITERION READS, and excludes NINE providers that do reach the engine.
      (Two of those three were the ones anybody had noticed; `dbsnp` fell out
      of the table itself once the roles were written down side by side, which
      is the only thing this artefact has done that reading prose did not.) THAT DISAGREEMENT IS
      LEFT VISIBLE AND UNRECONCILED ON PURPOSE: it is either correct for its
      purpose (it is the REPORT's raw-evidence display, a different question
      from classification) or a defect, AND NOBODY HAS RULED. Reconciling it
      here would make that ruling silently, on behalf of someone who never saw
      the question. `test_provider_roles.py` asserts the disagreement still
      holds, so it cannot be closed by accident.

THIS FILE IS INERT BY CONSTRUCTION, NOT BY CARE. It holds strings. It imports
no provider module, constructs nothing, and no production code imports it (a
test asserts that). Construction ORDER and EAGERNESS in
`GeperPipeline.__init__` may be load-bearing, so nothing here is allowed to
become the construction path: a registry that built providers would be a
BEHAVIOUR CHANGE WEARING A REFACTOR'S CLOTHES. This is a declaration checked
AGAINST the code by `geper/tests/test_provider_roles.py`, never a mechanism
the code depends on.

Derived from the code at master `4881302` (orchestrator.py:256-347,
acmg_rules.py:798-820, stage_schemas.py:220-254). If those move, the test
fails -- which is the point.
"""

import enum
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple


class ProviderRole(str, enum.Enum):
    """
    The four questions "which providers?" turns out to mean. Deliberately four
    separate members rather than one ranked scale: a provider can hold any
    combination, and every combination below is actually observed.
    """

    #: Assigned in `GeperPipeline.__init__`. Says nothing about who reads it.
    CONSTRUCTED_IN_ORCHESTRATOR = "constructed_in_orchestrator"
    #: A `*_result` parameter that `acmg_rules.evaluate()` DECLARES.
    REACHES_EVALUATE = "reaches_evaluate"
    #: A criterion method actually takes it. NOT implied by REACHES_EVALUATE
    #: -- `dbsnp` holds the second and not this one, and that gap is the point.
    READ_BY_A_CRITERION = "read_by_a_criterion"
    #: A required field of `stage_schemas.RawEvidenceBundle`.
    IN_RAW_EVIDENCE_BUNDLE = "in_raw_evidence_bundle"


@dataclass(frozen=True)
class ProviderRecord:
    """One provider. Strings and roles only -- no classes, no constructors."""

    key: str
    #: The `self.X` name in `__init__`, or None when constructed elsewhere.
    attribute: Optional[str]
    #: The method the orchestrator calls on it, as written. None when this
    #: provider does not reach the pipeline through a direct call.
    call: Optional[str]
    roles: FrozenSet[ProviderRole]
    note: str = ""


_C = ProviderRole.CONSTRUCTED_IN_ORCHESTRATOR
_E = ProviderRole.REACHES_EVALUATE
_R = ProviderRole.READ_BY_A_CRITERION
_B = ProviderRole.IN_RAW_EVIDENCE_BUNDLE


PROVIDER_ROLES: Tuple[ProviderRecord, ...] = (
    # ---- constructed, reaches the engine, read by a criterion -------------
    ProviderRecord("clinvar", "clinvar_client", "query_variant", frozenset({_C, _E, _R, _B})),
    ProviderRecord("gnomad", "gnomad_client", "query_variant", frozenset({_C, _E, _R, _B})),
    ProviderRecord("clingen", "clingen_client", "query_variant", frozenset({_C, _E, _R, _B})),
    ProviderRecord("uniprot", "uniprot_client", "query_variant", frozenset({_C, _E, _R, _B})),
    ProviderRecord("interpro", "interpro_client", "query_variant", frozenset({_C, _E, _R, _B})),
    ProviderRecord("conservation", "conservation_client", "query_variant", frozenset({_C, _E, _R})),
    ProviderRecord("transcript", "transcript_client", "query_variant", frozenset({_C, _E, _R})),
    ProviderRecord("hpo", "hpo_client", "query_variant", frozenset({_C, _E, _R})),
    ProviderRecord("functional_evidence", "functional_evidence_client", "query_variant", frozenset({_C, _E, _R})),
    ProviderRecord(
        "clinvar_codon",
        "clinvar_codon_client",
        "query_codon",
        frozenset({_C, _E, _R}),
        "keyed on a CODON, not a variant -- genuinely not the shared interface, do not normalise it",
    ),
    # ---- constructed, declared to the engine, read by NO criterion --------
    ProviderRecord(
        "dbsnp",
        "dbsnp_client",
        "lookup_variant",
        frozenset({_C, _E, _B}),
        "THE DEFECT THIS TABLE CANNOT FIND ON ITS OWN: accepted by evaluate(), read by no "
        "criterion; its only read is the _evidence_query_results ERROR gate, so a dbSNP "
        "failure can downgrade interpretation_outcome for a source no criterion consults. "
        "Same call shape as the other lookups under a different name.",
    ),
    # ---- constructed, reaches NO criterion (report/provenance surfaces) ---
    ProviderRecord(
        "blast",
        "blast_client",
        "search / search_many / retrieval_counts / mode",
        frozenset({_C, _B}),
        "FOUR surfaces including batch prefetch and a provenance accessor -- the real "
        "interface outlier, and required by RawEvidenceBundle while no criterion reads it",
    ),
    ProviderRecord(
        "alphafold",
        "alphafold_client",
        "query_variant",
        frozenset({_C, _B}),
        "required by RawEvidenceBundle; no criterion reads it",
    ),
    ProviderRecord("orphanet", "orphanet_client", "query_variant", frozenset({_C})),
    ProviderRecord("indigenomes", "indigenomes_client", "query_variant", frozenset({_C})),
    ProviderRecord("thousand_genomes_sas", "thousand_genomes_sas_client", "query_variant", frozenset({_C})),
    # ---- constructed under a NON-`_client` name --------------------------
    ProviderRecord(
        "protein",
        "protein_translator",
        "translate_context",
        frozenset({_C, _E, _R, _B}),
        "READ BY PVS1 ONLY. NOT by PM4/BP3, which take transcript_result/uniprot_result: "
        "acmg_rules.py deliberately computes protein effect via pvs1/utils.protein_effect_flags "
        "and names this module's frame-unaware window UNUSABLE for it. Excluded from reading (a) "
        "purely because it is not called `*_client`.",
    ),
    ProviderRecord("ensemble", "ensemble_manager", None, frozenset({_C, _E, _R})),
    ProviderRecord(
        "phenotype",
        "phenotype_result",
        None,
        frozenset({_C, _E, _R}),
        "held as run state rather than queried per variant",
    ),
    # ---- NOT constructed in the orchestrator at all ----------------------
    ProviderRecord(
        "alphamissense",
        None,
        None,
        frozenset({_E, _R, _B}),
        "arrives model-side, not through the constructor -- one of THREE construction paths "
        "feeding evaluate(), which is why a table scoped to __init__ covers only part",
    ),
    ProviderRecord("mmsplice", None, None, frozenset({_E, _R, _B})),
    ProviderRecord("spliceformer", None, None, frozenset({_E, _R})),
    ProviderRecord("splicebert", None, None, frozenset({_E, _R})),
)


def keys_with_role(role: ProviderRole) -> Tuple[str, ...]:
    """The providers holding `role`, sorted. Every published count is one of
    these -- 16 constructed, 18 declared to evaluate(), 17 read by a criterion,
    11 in the bundle -- so no two of them can drift apart."""
    return tuple(sorted(r.key for r in PROVIDER_ROLES if role in r.roles))


def roles_of(key: str) -> FrozenSet[ProviderRole]:
    for record in PROVIDER_ROLES:
        if record.key == key:
            return record.roles
    raise KeyError(f"no provider recorded under '{key}'. Known: {sorted(r.key for r in PROVIDER_ROLES)}")
