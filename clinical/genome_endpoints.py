"""
clinical/genome_endpoints.py
────────────────────────────
GENOME ANALYSIS, PHASE 1 -- chromosome-level view of small variants. Two
routers, one summariser (`clinical.genome_analysis`), no SQL and no role check
here.

    CLINICIAN GET /clinic/patients/{patient_id}/genome/chromosomes
              GET /clinic/patients/{patient_id}/genome/chromosomes/{name}
              GET /clinic/patients/{patient_id}/genome/map                (Phase 2)
              GET /clinic/patients/{patient_id}/genome/map/{name}?start&end (Phase 2)
    PATIENT   GET /portal/me/genome/chromosomes            (OFF BY DEFAULT)
              GET /portal/me/genome/chromosomes/{name}     (OFF BY DEFAULT)

THE PATIENT ROUTES ARE NOT MOUNTED unless `CLINICAL_ENABLE_PATIENT_GENOME` is
set (see `patient_routes_enabled`, read once in clinical/app.py). Unmounted, they
answer exactly what any unknown path answers.

AND WHEN MOUNTED THEY STILL SERVE NO PATIENT GENOMIC DATA. The response is
`clinical.genome_analysis.patient_projection`: static reference metadata for the
build (chromosome names, lengths, accessions; autosomes and MT only) and a
statement that findings are withheld. No variant, coordinate, allele, count,
per-chromosome status, provenance or classification, and nothing about X or Y.

WHY, STATED ONCE. `reports` has no content column, so a released report cannot
be mapped to the variants it discusses; the only variant source is
`interpretations.run_document`, which is raw pipeline output that the human
review can override and that includes variants the reviewer scoped out.
Report release authorises the report document, not that. Consent scopes are
free text with no scope for patient viewing, and nothing in the portal checks
withdrawal. GEPER_CLINICAL_PLATFORM_SPEC.md §16 recommends no patient access in
v1 and its counselling call-out is unanswered (ISO 15189 7.4.1.4 d). Exact
coordinates are directly resolvable in public databases, and Y-chromosome
content bears on the PCPNDT constraint (§5.3) for which the platform has no
prenatal flag. None of these is resolved here, and none is bypassed here.

THE PATIENT ROUTES take no patient identifier in any channel -- identity is the
authenticated patient session, via the portal's own `_get_patient_session`.
`{name}` is a chromosome name, normalised by `normalize_contig`; anything that is
not chromosome 1-22 or MT (X and Y are withheld) is a 404.

THE CLINICIAN ROUTES are org-scoped and role-gated inside the data layer
(Orderer, Interpreter or Approver), AUDITED as `genome_read` on the patient with
no payload, and show every non-tombstoned analysis for the patient with its
report states and a `released_to_patient` flag. They return the full
chromosome-level summary and, on the detail route, the variants.

ONE ANALYSIS PER INTERPRETATION, NEVER MERGED. A patient's orders may be on
different genome builds; each entry carries its own `assembly`.

NO LOGGING of genomic content from this module. The only log line the service
emits for these routes is uvicorn's access line, whose query string is redacted
by clinical/log_redaction.py; these routes accept no query parameters.

NOT HERE: CNV, structural variants, karyotype. Every response says they are
`not_assessed`; none is computed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import (
    AuthorizationError,
    DataAccess,
    GenomeConsentWithheldError,
    NotFoundError,
    PatientSession,
    Session,
)
from clinical.endpoints import _anonymous_refusal, _get_data_access, _get_session
from clinical.genome_analysis import (
    PATIENT_WITHHELD_CHROMOSOMES,
    CallSet,
    build_chromosome_map,
    normalize_contig,
    patient_projection,
    summarise_run_document,
)
from clinical.portal_endpoints import _get_patient_session

PATIENT_GENOME_ENV = "CLINICAL_ENABLE_PATIENT_GENOME"

portal_genome_router = APIRouter(prefix="/portal/me/genome", tags=["portal"])
clinic_genome_router = APIRouter(prefix="/clinic/patients", tags=["clinic"])

_GENOME_NOT_FOUND = "Not found."


def patient_routes_enabled(environ: Dict[str, str] | None = None) -> bool:
    """True only for an explicit 1/true/yes; absence, empty and anything else are OFF."""
    env = os.environ if environ is None else environ
    return env.get(PATIENT_GENOME_ENV, "").strip().lower() in {"1", "true", "yes"}


# The analysis dicts are typed loosely on purpose: their shape is owned by
# `patient_projection` / `AnalysisSummary` and pinned by their tests.
class PatientGenomeResponse(BaseModel):
    analyses: List[Dict[str, Any]]


class PatientChromosomeResponse(BaseModel):
    chromosome: str
    analyses: List[Dict[str, Any]]


class ClinicGenomeResponse(BaseModel):
    patient_id: str
    analyses: List[Dict[str, Any]]


class ClinicMapResponse(BaseModel):
    patient_id: str
    chromosome: Optional[str] = None
    maps: List[Dict[str, Any]]
    call_sets_note: str
    # Validated call sets that exist for this patient but are NOT drawn, by reason (a fixed vocabulary). Empty means none
    # are held back; it never means "none exist". Counts only: no id, coordinate or content.
    held_back_call_sets: Dict[str, int] = {}


class ClinicChromosomeResponse(BaseModel):
    patient_id: str
    chromosome: str
    analyses: List[Dict[str, Any]]


def _consent_withheld() -> HTTPException:
    """403 with its OWN message, so a clinician can tell "consent is not active" from "you lack the role"."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DataAccess.GENOME_CONSENT_WITHHELD)


def _bad_chromosome() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_GENOME_NOT_FOUND)


def _patient_entry(item: Dict[str, Any], name: str | None) -> Dict[str, Any]:
    """Allow-list only. `item["run_document"]` is read to learn the build and
    is never passed through; `report_ids` and `released_at` are release facts."""
    view = patient_projection(summarise_run_document(item["run_document"]))
    if name is not None:
        view["reference_chromosomes"] = [c for c in view["reference_chromosomes"] if c["name"] == name]
    return {
        "report_ids": [str(r) for r in item["report_ids"]],
        "released_at": item["released_at"].isoformat(),
        **view,
    }


def _clinic_entry(item: Dict[str, Any], name: str | None) -> Dict[str, Any]:
    summary = summarise_run_document(item["run_document"])
    body = summary.chromosome_to_dict(name) if name is not None else summary.to_dict()
    return {
        "interpretation_id": str(item["interpretation_id"]),
        "created_at": item["created_at"].isoformat(),
        "report_states": item["report_states"],
        "released_to_patient": item["released_to_patient"],
        **body,
    }


@portal_genome_router.get(
    "/chromosomes",
    response_model=PatientGenomeResponse,
    summary="Reference chromosomes for the authenticated patient's own released analyses (findings withheld)",
)
async def list_my_genome(
    patient_session: PatientSession = Depends(_get_patient_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientGenomeResponse:
    """NO IDENTIFIER OF ANY KIND; see the module docstring."""
    try:
        items = dao.list_my_genome_analyses(patient_session)
    except AuthorizationError:
        raise _anonymous_refusal()
    return PatientGenomeResponse(analyses=[_patient_entry(i, None) for i in items])


@portal_genome_router.get(
    "/chromosomes/{name}",
    response_model=PatientChromosomeResponse,
    summary="One reference chromosome of the authenticated patient's own released analyses (findings withheld)",
)
async def get_my_chromosome(
    name: str,
    patient_session: PatientSession = Depends(_get_patient_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientChromosomeResponse:
    canonical = normalize_contig(name)
    if canonical is None or canonical in PATIENT_WITHHELD_CHROMOSOMES:
        raise _bad_chromosome()
    try:
        items = dao.list_my_genome_analyses(patient_session)
    except AuthorizationError:
        raise _anonymous_refusal()
    return PatientChromosomeResponse(chromosome=canonical, analyses=[_patient_entry(i, canonical) for i in items])


@clinic_genome_router.get(
    "/{patient_id}/genome/chromosomes",
    response_model=ClinicGenomeResponse,
    summary="Chromosome-level small-variant summary for one patient in the CALLER'S OWN organisation (read-only)",
)
async def clinic_list_genome(
    patient_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ClinicGenomeResponse:
    target = _target(patient_id)
    try:
        items = dao.list_patient_genome_analyses(session, target)
    except GenomeConsentWithheldError:
        raise _consent_withheld()
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return ClinicGenomeResponse(patient_id=str(target), analyses=[_clinic_entry(i, None) for i in items])


@clinic_genome_router.get(
    "/{patient_id}/genome/chromosomes/{name}",
    response_model=ClinicChromosomeResponse,
    summary="One chromosome of one patient's analyses in the CALLER'S OWN organisation, with its variants (read-only)",
)
async def clinic_get_chromosome(
    patient_id: str,
    name: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ClinicChromosomeResponse:
    # The role gate must precede every other answer (403 is a function of roles
    # alone), so the chromosome name is checked AFTER the data layer's gate:
    # an unauthorised caller gets 403 whatever they typed.
    target = _target(patient_id)
    try:
        items = dao.list_patient_genome_analyses(session, target)
    except GenomeConsentWithheldError:
        raise _consent_withheld()
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    canonical = normalize_contig(name)
    if canonical is None:
        raise _bad_chromosome()
    return ClinicChromosomeResponse(
        patient_id=str(target), chromosome=canonical, analyses=[_clinic_entry(i, canonical) for i in items]
    )


# ── Phase 2: the Chromosome Explorer map (clinician only) ────────────────────
#
# WHERE CNV/SV CALLS COME FROM. Stored call sets (migration 0010), read through ONE data-layer method that returns
# only VALIDATED, live, non-redacted evidence of THIS organisation's patient whose assembly agrees with the ordered
# test (`DataAccess.list_patient_validated_call_sets`). The route supplies the session and the path's patient id and
# nothing else: the organisation is the session's, and the patient is verified in the data layer against it. Each
# stored document is rebuilt through the validating models, so a stored document that no longer satisfies them is
# not returned by the data layer, which records a `call_set_integrity_failure` audit row (id only) instead.
# Tests override the dependency with synthetic call sets.


class DaoCallSetSource:
    """The stored-call-set source: delegates entirely to the data layer, which owns every check."""

    def __init__(self, dao: DataAccess) -> None:
        self._dao = dao
        self._excluded: Dict[str, int] = {}

    def call_sets_for(self, session: Session, patient_id: Any) -> Sequence[CallSet]:
        evidence = self._dao.patient_call_set_evidence(session, patient_id)
        self._excluded = dict(evidence["excluded"])
        return [row["call_set"] for row in evidence["eligible"]]

    def excluded_for(self, session: Session, patient_id: Any) -> Dict[str, int]:
        """Valid only after `call_sets_for` in the same request: the same single read supplies both."""
        return dict(getattr(self, "_excluded", {}))


def get_call_set_source(dao: DataAccess = Depends(_get_data_access)) -> DaoCallSetSource:
    return DaoCallSetSource(dao)


_CALL_SETS_NOTE_NONE = (
    "No CNV or structural-variant call set is attached to this patient, so those layers are not assessed. "
    "This is not a finding that no such variants exist."
)
_CALL_SETS_NOTE_SOME = "Call sets are drawn only on a map of their own assembly; none is converted between assemblies."
_EXCLUDED_WORDS = {
    "consent_withdrawn": "consent is withdrawn",
    "sample_qc_not_passed": "the sample has not passed QC",
    "order_cancelled": "the order is cancelled",
    "sample_identity_changed": "the sample no longer matches the run",
    "assembly_mismatch": "the assembly does not match the ordered test",
    "small_variant_build_mismatch": "the run's small-variant analysis is on another genome build",
    "integrity_failure": "its stored evidence failed an integrity check",
    "retention_redacted": "it was removed under the retention policy",
}


def _call_sets_note(call_sets: Sequence[Any], excluded: Dict[str, int]) -> str:
    """Three different answers: drawn, held back (and why), and never attached. Reasons are a fixed vocabulary."""
    if not excluded:
        return _CALL_SETS_NOTE_SOME if call_sets else _CALL_SETS_NOTE_NONE
    total = sum(excluded.values())
    why = "; ".join(f"{n} because {_EXCLUDED_WORDS.get(k, k)}" for k, n in sorted(excluded.items()))
    held = (
        f"{total} validated CNV/structural-variant call set(s) for this patient are NOT shown ({why}). "
        "Those layers are not assessed from them; this is not a finding that no such variants exist."
    )
    return f"{_CALL_SETS_NOTE_SOME} {held}" if call_sets else held


def _excluded_of(source: Any, session: Session, patient: Any) -> Dict[str, int]:
    getter = getattr(source, "excluded_for", None)
    return dict(getter(session, patient)) if callable(getter) else {}


def _parse_region(start: Optional[str], end: Optional[str]) -> Optional[tuple]:
    if start is None and end is None:
        return None
    try:
        s, e = int(start), int(end)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid region.")
    if s < 1 or e < s:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid region.")
    return s, e


def _maps(
    items: List[Dict[str, Any]], call_sets: Sequence[CallSet], only: Optional[str], region
) -> List[Dict[str, Any]]:
    """One map per analysis (its own build) plus one per call-set build no analysis covers. Never merged."""
    out: List[Dict[str, Any]] = []
    covered = set()
    for item in items:
        summary = summarise_run_document(item["run_document"])
        head = {
            "interpretation_id": str(item["interpretation_id"]),
            "created_at": item["created_at"].isoformat(),
            "report_states": item["report_states"],
            "released_to_patient": item["released_to_patient"],
        }
        if summary.assembly is None or summary.status != "ok":
            out.append({**head, "status": summary.status, "reason": summary.reason, "map": None})
            continue
        covered.add(summary.assembly)
        out.append(
            {
                **head,
                "status": "ok",
                "reason": None,
                "map": build_chromosome_map(
                    assembly=summary.assembly, summary=summary, call_sets=call_sets, only_chromosome=only, region=region
                ),
            }
        )
    for build in sorted({cs.assembly for cs in call_sets} - covered):
        out.append(
            {
                "interpretation_id": None,
                "created_at": None,
                "report_states": [],
                "released_to_patient": False,
                "status": "ok",
                "reason": "no small-variant analysis exists for this assembly",
                "map": build_chromosome_map(
                    assembly=build, summary=None, call_sets=call_sets, only_chromosome=only, region=region
                ),
            }
        )
    return out


@clinic_genome_router.get(
    "/{patient_id}/genome/map",
    response_model=ClinicMapResponse,
    summary="Chromosome Explorer data for one patient: small variants, CNV and SV layers per assembly (read-only)",
)
async def clinic_genome_map(
    patient_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
    source: DaoCallSetSource = Depends(get_call_set_source),
) -> ClinicMapResponse:
    target = _target(patient_id)
    try:
        items = dao.list_patient_genome_analyses(session, target)
        call_sets = list(source.call_sets_for(session, target))
    except GenomeConsentWithheldError:
        raise _consent_withheld()
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    excluded = _excluded_of(source, session, target)
    return ClinicMapResponse(
        patient_id=str(target),
        maps=_maps(items, call_sets, None, None),
        call_sets_note=_call_sets_note(call_sets, excluded),
        held_back_call_sets=excluded,
    )


@clinic_genome_router.get(
    "/{patient_id}/genome/map/{name}",
    response_model=ClinicMapResponse,
    summary="One chromosome of the Chromosome Explorer data, optionally restricted to start..end (read-only)",
)
async def clinic_genome_map_chromosome(
    patient_id: str,
    name: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
    source: DaoCallSetSource = Depends(get_call_set_source),
) -> ClinicMapResponse:
    # `start`/`end` are strings and are parsed AFTER the data layer's gate, so a
    # malformed region cannot answer before the role check (no 422 ahead of a 403).
    target = _target(patient_id)
    try:
        items = dao.list_patient_genome_analyses(session, target)
        call_sets = list(source.call_sets_for(session, target))
    except GenomeConsentWithheldError:
        raise _consent_withheld()
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    canonical = normalize_contig(name)
    if canonical is None:
        raise _bad_chromosome()
    region = _parse_region(start, end)
    return ClinicMapResponse(
        patient_id=str(target),
        chromosome=canonical,
        maps=_maps(items, call_sets, canonical, region),
        call_sets_note=_call_sets_note(call_sets, _excluded_of(source, session, target)),
        held_back_call_sets=_excluded_of(source, session, target),
    )
