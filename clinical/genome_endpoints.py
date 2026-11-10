"""
clinical/genome_endpoints.py
────────────────────────────
GENOME ANALYSIS, PHASE 1 -- chromosome-level view of small variants. Two
routers, one summariser (`clinical.genome_analysis`), no SQL and no role check
here.

    CLINICIAN GET /clinic/patients/{patient_id}/genome/chromosomes
              GET /clinic/patients/{patient_id}/genome/chromosomes/{name}
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
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import (
    AuthorizationError,
    DataAccess,
    NotFoundError,
    PatientSession,
    Session,
)
from clinical.endpoints import _anonymous_refusal, _get_data_access, _get_session
from clinical.genome_analysis import (
    PATIENT_WITHHELD_CHROMOSOMES,
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


class ClinicChromosomeResponse(BaseModel):
    patient_id: str
    chromosome: str
    analyses: List[Dict[str, Any]]


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
