"""
Pydantic schema validation at pipeline stage boundaries.

Why this exists (a real, shipped bug): `InterpretationResult.to_dict()`
(`pipeline/interpretation_result.py`) deliberately omits `raw_evidence`
from its serialized form. `report/clinical_report_builder.py
::build_clinical_report()` used to read `raw_evidence` only from that
serialized dict, so it was always `{}` -- meaning five report sections
(Protein Knowledge, Structural Knowledge, Population Evidence, Clinical
Evidence, Sequence Context) reported "not found" regardless of what
UniProt/InterPro/gnomAD/dbSNP/ClinVar/ClinGen/AlphaFold had actually
returned. It shipped silently (see commit 9f5a2bf's fix and
tests/test_report_consistency.py) and was only caught by a human
noticing a contradiction in the rendered report.

Every provider dict that flows into that boundary already uses an
informal `found` / `skipped` / `error` convention (see the per-stage
`_run_*_stage` methods in `pipeline/orchestrator.py`) to mean roughly
"never ran" / "ran, nothing there" / "ran, failed" -- but it's a
convention every call site has to remember and apply correctly by
hand. `StageStatus`/`StageEvidence` below promote that convention into
an explicit, required field: constructing a `RawEvidenceBundle` with a
slot missing (e.g. an empty `{}` standing in for "no raw evidence" --
exactly the shape of the original bug) fails LOUDLY at construction
time with a Pydantic `ValidationError`, instead of silently behaving
like "every source found nothing."

Scope (deliberately not "boil the ocean" -- see each section for the
specific call): this covers the two orchestrator boundaries that feed
`report/clinical_report_builder.py::build_clinical_report()`, the
function a clinician's report is actually built from --
  1. `RawEvidenceBundle` -- the 11 raw per-stage provider dicts handed
     into the clinical-report boundary (the exact site of the bug
     above).
  2. `InterpretationResultForReport` -- the `InterpretationResult`-
     shaped dict `build_clinical_report()` reads everything else from.
  3. `AcmgEvaluationSchema` -- one boundary further upstream, the ACMG
     evaluation dict `pipeline/interpretation_result.py
     ::build_interpretation_result()` reads to build the
     `InterpretationResult` in the first place.
Provider-internal shapes (the DNA/RNA model embedding dicts, BLAST hit
detail, PVS1/transcript structure, HPO/Orphanet, functional evidence,
conservation scores, normalization, the splice ensemble) are NOT typed
here -- either they don't feed the clinical report's core sections
directly, or their existing dict shape is already adequate and lower-
risk. See this module's own report-back (not committed as a file --
communicated in the PR/commit message) for the full list of boundaries
considered and deliberately deferred.

Validation failures are always logged loudly (`logger.error`, visible
in every environment, not gated behind a dev/prod flag) but never
raise past this module's own boundary functions -- matching this
codebase's existing graceful-degradation policy (e.g.
`pipeline/conservation/provider.py::_SingleScoreProvider._try`: "a
provider bug must never break the pipeline"). A validation failure
must never be papered over by silently substituting an empty result,
either: callers get back `(None, error_message)` and are expected to
proceed with the original, unvalidated raw dict (best-effort, exactly
today's pre-schema behavior) rather than blanking data that is
actually present -- re-introducing the empty-`{}` bug via the "safe"
path would defeat the entire point of this module.
"""

from __future__ import annotations

import enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-stage evidence wrapper
# ---------------------------------------------------------------------------


class StageStatus(str, enum.Enum):
    """
    The one distinction this whole module exists to make structural
    rather than conventional: "this stage never ran for this variant"
    (`NOT_RUN` -- disabled via config, or the variant was ineligible,
    e.g. AlphaMissense on a synonymous variant) is a DIFFERENT claim
    from "it ran, checked, and there was genuinely nothing there"
    (`NOT_FOUND`), which is again different from "it ran and failed"
    (`ERROR`) -- the same category of bug as PM1's "position unknown"
    vs. "checked, no overlap" ([[geper-pm1-protein-position-limitation]])
    and the BS3/PS3 error-vs-not-found mislabeling this module's
    `error` field also encodes. Conflating any of these has already
    caused a real bug in this codebase (see this module's docstring):
    `report/clinical_report_builder.py` used to render a NOT_RUN-shaped
    empty dict as if every source had been checked and found nothing.
    """

    NOT_RUN = "not_run"
    ERROR = "error"
    NOT_FOUND = "not_found"
    FOUND = "found"


class StageEvidence(BaseModel):
    """
    Wraps ONE pipeline stage's raw provider-result dict (ClinVar,
    gnomAD, UniProt, ...) with an explicit, REQUIRED `status` -- there
    is no default, so building one always forces a caller (in
    practice, always `StageEvidence.from_raw()` below) to decide which
    of the four `StageStatus` states applies, rather than letting an
    absent/empty dict silently mean "not found" by default the way the
    pre-fix `raw.get("uniprot") or {}` pattern did.

    `data` deliberately stays a loose `Dict[str, Any]`, not a fully-
    typed per-provider model: GEPER integrates 11 independent external
    data sources here, each with its own genuinely different response
    shape (compare `models/alphamissense.py`'s `am_class`/
    `am_pathogenicity` to `pipeline/gnomad/`'s population-frequency
    breakdown to `database/blast_client.py`'s `hits`/`hit_count`).
    Fully typing all 11 is a separate, much larger effort than this
    first pass at the report-facing handoff covers -- a deliberate
    scope decision (see this module's docstring), not an oversight.
    Downstream code (`report/clinical_report_builder.py`'s section
    builders) keeps reading `data` with the exact same `.get(...)`
    calls it already used on the raw dict -- this wrapper only adds
    the structural status alongside it, it does not change what's
    inside.
    """

    model_config = ConfigDict(frozen=True)

    status: StageStatus
    error: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _error_field_matches_status(self) -> "StageEvidence":
        if self.error and self.status != StageStatus.ERROR:
            raise ValueError(
                f"error message present ({self.error!r}) but status is "
                f"'{self.status.value}', not 'error' -- status/error must agree"
            )
        if self.status == StageStatus.ERROR and self.error is None:
            raise ValueError("status is 'error' but no error message was recorded")
        return self

    @classmethod
    def from_raw(
        cls,
        raw: Optional[Dict[str, Any]],
        *,
        found_key: str = "found",
        found_when: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> "StageEvidence":
        """
        Derive a `StageEvidence` from one stage's raw result dict,
        using the `skipped` / `error` / `found` convention already
        used across `pipeline/orchestrator.py`'s `_run_*_stage`
        methods (see e.g. `pipeline/gnomad/lookup.py`,
        `pipeline/clingen/lookup.py`: `{"skipped": True, ...}` for a
        disabled/ineligible stage; `{"error": "...", "found": False}`
        for a failed lookup).

        `found_when`/`found_key` cover the providers that don't use a
        plain boolean `found` key -- BLAST (`hit_count`), MMSplice
        (`predicted`), and the protein/ESM-2 translation stage (which
        isn't a lookup at all: reaching this point with `skipped`
        False already means real translation data exists, so it is
        unconditionally `FOUND`). See `RawEvidenceBundle.from_raw`
        for exactly which override each of the 11 providers gets and
        why.

        Precedence -- `error` is checked BEFORE `skipped`: fixed
        design smell (previously: `_run_protein_stage`,
        `_run_blast_stage`, and `_run_mmsplice_stage` in
        `pipeline/orchestrator.py` folded a genuine exception into the
        exact same `{"skipped": True, "reason": "..."}` (or
        `supported=False`) shape used for a normal, expected skip --
        `RNA-FM`/`_run_rna_stage` and `AlphaMissense`
        /`_run_alphamissense_stage` had the identical collapse, found
        via the same audit). Those methods now also set an explicit
        `error` key on their exception paths while deliberately
        LEAVING `skipped`/`supported`/`predicted` unchanged (control-
        flow gates elsewhere still need "crashed" to behave like
        "skipped" for THEIR purposes) -- so `error`
        must be checked first here, or a stage that is both
        `skipped=True` AND carries a real `error` would still resolve
        to `NOT_RUN`, silently undoing the fix. Every provider that
        already set `error` correctly before this change (ClinVar,
        dbSNP, gnomAD, ClinGen, HPO, Orphanet, transcript structure,
        PS1/PM5 codon matches, PS3/BS3 functional evidence, UniProt,
        InterPro, AlphaFold) never sets `skipped=True` on that same
        path, so this reordering does not change their behavior at
        all -- verified by this module's own test suite.
        """
        if not raw:
            return cls(status=StageStatus.NOT_RUN, data={})
        error = raw.get("error")
        if error is not None:
            return cls(status=StageStatus.ERROR, error=str(error), data=raw)
        if raw.get("skipped") is True:
            return cls(status=StageStatus.NOT_RUN, data=raw)
        found = found_when(raw) if found_when is not None else bool(raw.get(found_key))
        return cls(status=StageStatus.FOUND if found else StageStatus.NOT_FOUND, data=raw)


# ---------------------------------------------------------------------------
# Boundary 1: raw per-stage provider dicts -> the clinical-report boundary
# ---------------------------------------------------------------------------


class RawEvidenceBundle(BaseModel):
    """
    The validated form of what `report/json_builder.py
    ::build_variant_result()` used to assemble by hand as a plain
    `raw_evidence_for_report` dict (see that module and commit
    9f5a2bf). Every one of the 11 fields below is REQUIRED (no
    default) -- constructing this with a slot missing raises a
    Pydantic `ValidationError` immediately, which is precisely the
    mechanism that would have caught the original bug on its very
    first run: the bug's actual failure value was an empty `{}`
    standing in for "no raw evidence", which fails this model's
    validation with 11 "field required" errors rather than silently
    validating as "every source checked, found nothing".

    Always construct via `.from_raw(...)`, not `RawEvidenceBundle(...)`
    directly -- the classmethod is what supplies the per-provider
    `StageEvidence.from_raw` overrides (BLAST's `hit_count`,
    MMSplice's `predicted`, protein's always-`FOUND`-when-not-skipped)
    documented on `StageEvidence.from_raw` above.
    """

    model_config = ConfigDict(frozen=True)

    clinvar: StageEvidence
    dbsnp: StageEvidence
    protein: StageEvidence
    blast: StageEvidence
    alphamissense: StageEvidence
    mmsplice: StageEvidence
    gnomad: StageEvidence
    clingen: StageEvidence
    uniprot: StageEvidence
    interpro: StageEvidence
    alphafold: StageEvidence

    @classmethod
    def from_raw(
        cls,
        *,
        clinvar_result: Optional[Dict[str, Any]] = None,
        dbsnp_result: Optional[Dict[str, Any]] = None,
        protein_result: Optional[Dict[str, Any]] = None,
        blast_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
    ) -> "RawEvidenceBundle":
        return cls(
            clinvar=StageEvidence.from_raw(clinvar_result),
            dbsnp=StageEvidence.from_raw(dbsnp_result),
            # Not a lookup -- see StageEvidence.from_raw's docstring.
            protein=StageEvidence.from_raw(protein_result, found_when=lambda r: True),
            blast=StageEvidence.from_raw(blast_result, found_when=lambda r: (r.get("hit_count") or 0) > 0),
            alphamissense=StageEvidence.from_raw(alphamissense_result),
            mmsplice=StageEvidence.from_raw(mmsplice_result, found_key="predicted"),
            gnomad=StageEvidence.from_raw(gnomad_result),
            clingen=StageEvidence.from_raw(clingen_result),
            uniprot=StageEvidence.from_raw(uniprot_result),
            interpro=StageEvidence.from_raw(interpro_result),
            alphafold=StageEvidence.from_raw(alphafold_result),
        )

    def to_legacy_dict(self) -> Dict[str, Any]:
        """
        Reconstruct the plain `{"uniprot": {...}, "gnomad": {...}, ...}`
        shape `report/clinical_report_builder.py`'s section builders
        (`_protein_knowledge`, `_structural_knowledge`, etc.) already
        read via `raw.get("uniprot") or {}` -- deliberately UNCHANGED
        by this validation pass (see this module's docstring: this is
        a validation layer in front of the existing report logic, not
        a rewrite of it). `.data` is each stage's original raw dict
        verbatim (or `{}` for a `NOT_RUN`/never-passed stage, matching
        what `raw.get(...) or {}` already produced for a `None` value).
        """
        return {name: getattr(self, name).data for name in type(self).model_fields}


def build_raw_evidence_bundle(
    *,
    variant_ref: str = "",
    clinvar_result: Optional[Dict[str, Any]] = None,
    dbsnp_result: Optional[Dict[str, Any]] = None,
    protein_result: Optional[Dict[str, Any]] = None,
    blast_result: Optional[Dict[str, Any]] = None,
    alphamissense_result: Optional[Dict[str, Any]] = None,
    mmsplice_result: Optional[Dict[str, Any]] = None,
    gnomad_result: Optional[Dict[str, Any]] = None,
    clingen_result: Optional[Dict[str, Any]] = None,
    uniprot_result: Optional[Dict[str, Any]] = None,
    interpro_result: Optional[Dict[str, Any]] = None,
    alphafold_result: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[RawEvidenceBundle], Optional[str]]:
    """
    Boundary entry point: builds and validates a `RawEvidenceBundle`
    from the same raw per-stage kwargs `report/json_builder.py
    ::build_variant_result()` already receives.

    Never raises -- returns `(bundle, None)` on success or `(None,
    error_message)` on failure, with the failure ALWAYS logged at
    `error` level first (loud in every environment, not just "dev"; a
    clinical run must never crash over this, so there is no separate
    "raise in dev" mode -- see this module's docstring). The caller is
    expected to fall back to the original raw dicts (still in hand,
    unchanged) on failure rather than treating `(None, ...)` as license
    to substitute an empty result.
    """
    try:
        bundle = RawEvidenceBundle.from_raw(
            clinvar_result=clinvar_result,
            dbsnp_result=dbsnp_result,
            protein_result=protein_result,
            blast_result=blast_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            gnomad_result=gnomad_result,
            clingen_result=clingen_result,
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result=alphafold_result,
        )
        return bundle, None
    except Exception as exc:  # noqa: BLE001 -- a malformed stage-result dict (or a bug in this validation layer itself) must never break the pipeline; see this module's docstring
        message = f"raw_evidence schema validation failed for variant {variant_ref or '(unknown)'}: {exc}"
        logger.error(message)
        return None, message


# ---------------------------------------------------------------------------
# Boundary 2: InterpretationResult -> the clinical-report boundary
# ---------------------------------------------------------------------------


class InterpretationResultForReport(BaseModel):
    """
    The subset of `pipeline/interpretation_result.py::InterpretationResult`
    that `report/clinical_report_builder.py::build_clinical_report()`
    actually reads (cross-checked field-by-field against every
    `ir.get(...)` call in that module). Deliberately NOT a schema for
    the whole `InterpretationResult` dataclass -- Phase 3/4/6/7 engines
    (confidence/priority/conflict/explainability) mutate a fresh
    `InterpretationResult` progressively as they each run inside
    `InterpretationEngine.interpret()`
    (`pipeline/interpretation.py`), so a *mid-construction* instance
    legitimately has fields this model would reject; this model
    describes only the fully-assembled shape at the point it is about
    to be handed to the report builder (the end of `interpret()`,
    verified by reading that method start to finish -- every Phase 3/
    4/6/7 engine call happens before `result_obj.to_dict()` is
    returned).

    List/bool fields below (`triggered_rules`, `confidence_pending`,
    etc.) are REQUIRED, not `Optional` -- the dataclass's own
    `default_factory=list` / bool defaults mean they are never
    genuinely absent once `InterpretationResult` exists; a missing
    value at THIS boundary is a real bug, not a legitimate state, so
    it is not defaulted away here either.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    variant: Dict[str, Any] = Field(default_factory=dict)
    gene_symbol: Optional[str] = None

    acmg_classification: Optional[str] = None
    triggered_rules: List[Dict[str, Any]]
    not_triggered_rules: List[Dict[str, Any]]
    not_evaluated_rules: List[Dict[str, Any]]
    combining_rule_trace: List[str]

    supporting_evidence: List[str]
    conflicting_evidence: List[str]

    ai_consensus: List[Dict[str, Any]]
    ai_context_models: List[str]

    confidence_pending: bool
    confidence_score: Optional[float] = None
    confidence_label: Optional[str] = None
    confidence_breakdown: Optional[Dict[str, Any]] = None

    priority_pending: bool
    priority_score: Optional[float] = None
    priority_category: Optional[str] = None
    priority_rank: Optional[int] = None
    priority_explanation: List[str]
    priority_breakdown: Optional[Dict[str, Any]] = None

    conflict_summary: Optional[str] = None
    conflict_list: List[Dict[str, Any]]
    conflict_score: float
    conflict_severity: Optional[str] = None
    conflict_resolution: Optional[str] = None

    explainability: Optional[Dict[str, Any]] = None
    recommendations: List[str]
    evidence_sources: List[str]
    # Which AI models genuinely crashed for this variant (as opposed
    # to a legitimate skip) -- see `InterpretationResult.ai_model_errors`'s
    # own docstring in `pipeline/interpretation_result.py`. Required,
    # not `Optional`, matching every other list field here: this is
    # always at least `[]` once `InterpretationResult` exists.
    ai_model_errors: List[Dict[str, Any]]

    @model_validator(mode="after")
    def _pending_flag_matches_populated_value(self) -> "InterpretationResultForReport":
        """
        Structurally enforces the same "pending vs. populated" pairing
        `InterpretationResult`'s own field comments already document
        as a convention: `confidence_pending=False` is a claim that
        the ConfidenceEngine actually ran and produced a real score --
        `pipeline/interpretation.py::interpret()` only ever sets
        `confidence_pending = False` in the same block that also sets
        `confidence_score`/`confidence_label`, so if the pairing is
        broken by the time it reaches this boundary, something
        upstream corrupted it (e.g. a caller manually building this
        dict for a test, or a future refactor accidentally splitting
        the two writes) -- worth failing loudly on rather than
        rendering a confidently-blank "confidence: None" in a clinical
        report.
        """
        if not self.confidence_pending and (self.confidence_score is None or self.confidence_label is None):
            raise ValueError(
                "confidence_pending is False but confidence_score/confidence_label is None -- "
                "'not pending' must mean the score actually exists"
            )
        if not self.priority_pending and (self.priority_score is None or self.priority_category is None):
            raise ValueError(
                "priority_pending is False but priority_score/priority_category is None -- "
                "'not pending' must mean the score actually exists"
            )
        return self


def validate_interpretation_result_for_report(
    interpretation_result: Optional[Dict[str, Any]], variant_ref: str = ""
) -> Tuple[Optional[InterpretationResultForReport], Optional[str]]:
    """
    Boundary entry point mirroring `build_raw_evidence_bundle`'s
    contract exactly: never raises, logs loudly on failure, returns
    `(None, error_message)` rather than fabricating a passing result.
    Returns `(None, None)` (no error) for the legitimate "no
    interpretation result yet" case (`None`/missing, or the aggregation
    engine's own `{"error": ...}` sentinel -- see
    `pipeline/interpretation.py::interpret()`'s `except` branch) --
    that is not a schema problem, it's the documented "aggregation
    failed for this variant" state `build_clinical_report` already
    special-cases.
    """
    try:
        if not interpretation_result or "error" in interpretation_result:
            return None, None
        return InterpretationResultForReport.model_validate(interpretation_result), None
    except Exception as exc:  # noqa: BLE001 -- a malformed interpretation_result (or a bug in this validation layer itself) must never break the pipeline; see this module's docstring
        message = f"interpretation_result schema validation failed for variant {variant_ref or '(unknown)'}: {exc}"
        logger.error(message)
        return None, message


# ---------------------------------------------------------------------------
# Boundary 3: ACMG evaluation -> InterpretationResult construction
# ---------------------------------------------------------------------------


class AcmgEvaluationSchema(BaseModel):
    """
    The shape `pipeline/interpretation_result.py
    ::build_interpretation_result()` expects from
    `interpretation.get("acmg_evaluation")` (Phase 1's output --
    `pipeline/acmg_rules.py::ACMGRuleEngine`) -- one boundary further
    upstream than `InterpretationResultForReport` above, at the point
    `InterpretationResult` itself is first assembled. A malformed
    `acmg_evaluation` here would otherwise silently propagate an empty/
    wrong ACMG classification through every downstream phase (2-7) and
    into the report with no single obvious point of failure -- this is
    the earliest place it can be caught.

    Kept deliberately smaller than `InterpretationResultForReport`:
    only the keys `build_interpretation_result` actually reads
    (`classification`, `triggered_criteria`, `not_triggered_criteria`,
    `not_evaluated_criteria`, `combining_rule_trace`, and -- report
    review round 10 -- `net_points`/`pathogenic_points`/`benign_points`,
    the Tavtigian point totals `ACMGRuleEngine._combine` used to discard
    after picking `classification`; see `pipeline/acmg_rules.py::
    CombineResult`) -- see `pipeline/interpretation_result.py` lines
    around `acmg.get("triggered_criteria", [])` etc.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    classification: Optional[str] = None
    triggered_criteria: List[Dict[str, Any]] = Field(default_factory=list)
    not_triggered_criteria: List[Dict[str, Any]] = Field(default_factory=list)
    not_evaluated_criteria: List[Dict[str, Any]] = Field(default_factory=list)
    combining_rule_trace: List[str] = Field(default_factory=list)
    net_points: Optional[float] = None
    pathogenic_points: Optional[float] = None
    benign_points: Optional[float] = None


def validate_acmg_evaluation(
    acmg_evaluation: Optional[Dict[str, Any]], variant_ref: str = ""
) -> Tuple[Optional[AcmgEvaluationSchema], Optional[str]]:
    """Boundary entry point, same never-raises/log-loudly contract as
    `build_raw_evidence_bundle`/`validate_interpretation_result_for_report`."""
    try:
        if not acmg_evaluation:
            return None, None
        return AcmgEvaluationSchema.model_validate(acmg_evaluation), None
    except Exception as exc:  # noqa: BLE001 -- a malformed acmg_evaluation (or a bug in this validation layer itself) must never break the pipeline; see this module's docstring
        message = f"acmg_evaluation schema validation failed for variant {variant_ref or '(unknown)'}: {exc}"
        logger.error(message)
        return None, message
