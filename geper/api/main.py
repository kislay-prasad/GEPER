"""
api/main.py
────────────
Bij AI structural-annotation FastAPI application.

Endpoints
---------
GET /structures/{accession}  — AlphaFold DB structural annotation for one
                                 variant's mapped residue (the 3D viewer's
                                 data endpoint)

Scope, deliberately narrow: this app exists to serve the one endpoint above.
No health-check surface has been added beyond what that endpoint itself
needs to run -- still an open, separately-owned question (see the
2026-08-23 hive design/implementation dispatch).

Auth/CORS (2026-09-02): ported from kim_pipeline/api/main.py's fail-to-start
control (that app's GAP 2 / FIX #3) so both FastAPI apps in this monorepo
share one posture -- refuses to start without GEPER_API_KEYS +
GEPER_CORS_ORIGINS unless GEPER_DEV_INSECURE=1 is set explicitly. This app
had been left fail-open since the 2026-08-23 dispatch above: when this app
was split out as Option A, auth/CORS were deferred as an open question here
rather than reusing the control kim_pipeline had already shipped -- nobody
revisited it when this app was stood up, not a considered decision to run
without one.

Architecture note (why this lives in geper/, not kim_pipeline/api/): ruled
2026-08-23 as Option A. kim_pipeline/api/main.py is the monorepo's only
other FastAPI app, but kim_pipeline has no structural code and geper/ and
kim_pipeline/ run as separate subprocesses with no shared Python process
(see bridge/combined_pipeline.py's own docstring) -- serving this from
kim_pipeline would mean either creating an in-process dependency that does
not exist, or geper persisting structural data for kim_pipeline to read,
which is what today's pass-through design avoids. This endpoint lives next
to the data, the mapping gate, and the provenance that produce it.

RESOLVED (2026-08-26, human ruling, route c): `mapping_status` splits two
distinct causes of "found, but no residue-level confidence" --
`NO_POSITION` (no protein position was ever resolved for this variant) vs.
`NOT_MAPPABLE` (a real position existed but the mapping gate rejected it on
the merits: out of span / not modelled / fragment-numbering mismatch) --
using the raw result's own `protein_position` field: `None` means
NO_POSITION, non-None means NOT_MAPPABLE. This needed no `mapping_gate.py`
change and no reason-string pattern-matching, because `protein_position` is
echoed unconditionally by `provider.py::_build_annotation` regardless of the
gate's verdict -- the only two constructors reached along the `found=True`
path (`LocalDatasetAlphaFoldProvider.query`, `LiveAPIAlphaFoldProvider.query`)
both pass it through. The two non-echoing constructors,
`AlphaFoldAnnotation.not_found`/`.from_error`, never reach this branch at
all: they set `found=False`, which is handled above and mapped to
`EntryStatus.NOT_FOUND`/`ERROR` before `mapping_status` is ever considered.
Confirmed by execution, not by reading alone, before this was implemented
(constructed the case: a non-None `protein_position` the gate rejects for a
non-NO_POSITION reason, e.g. out-of-span -- the raw dict's `protein_position`
stayed the non-None value, never collapsed to `None`). See
`tests/test_api_structures.py`'s `TestMapToResponseFoundNoPosition`/
`TestMapToResponseFoundNotMappable` for the pinned fixtures.
"""

from __future__ import annotations

import enum
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pipeline.alphafold.lookup import AlphaFoldLookup

from .submission_store import SubmissionStore

logger = logging.getLogger("geper.structures_api")

# ─── API key authentication (ported from kim_pipeline/api/main.py's fail-to-
# start control -- see that module's GAP 2 / FIX #3 for the original) ────────


def _load_api_keys() -> Optional[Set[str]]:
    """Load valid API keys from GEPER_API_KEYS env var.

    Returns None if not set (dev mode — allow all requests).
    """
    raw = os.getenv("GEPER_API_KEYS", "")
    if not raw:
        return None
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    return keys if keys else None


_API_KEYS: Optional[Set[str]] = _load_api_keys()
_CORS_ORIGINS_ENV: Optional[str] = os.getenv("GEPER_CORS_ORIGINS")  # None if unset; "*" applied later
_DEV_INSECURE: bool = os.getenv("GEPER_DEV_INSECURE", "") == "1"

if _API_KEYS is None:
    logger.warning(
        "GEPER_API_KEYS is not set — running in dev mode with no authentication. "
        "Set GEPER_API_KEYS=key1,key2 before deploying to production."
    )


def _refuse_insecure_defaults_unless_opted_in() -> None:
    """A startup warning is not a control -- it arrives after the decision to
    run without auth/CORS restriction has already been made. Refuse to start
    with the insecure defaults (no auth, CORS "*") unless GEPER_DEV_INSECURE=1
    is set explicitly, so a deployer who simply forgets GEPER_API_KEYS/
    GEPER_CORS_ORIGINS gets a hard failure instead of a log line that's easy
    to miss on a deploy console.

    Same control kim_pipeline/api/main.py has shipped (FIX #3); this app
    never had it because its own docstring deferred auth/CORS as an "open
    question" when it was stood up (2026-08-23) rather than porting
    kim_pipeline's already-shipped fix -- see the divergence note sent to
    god alongside this diff.
    """
    if _DEV_INSECURE:
        return
    missing = [
        name
        for name, value in (
            ("GEPER_API_KEYS", _API_KEYS),
            ("GEPER_CORS_ORIGINS", _CORS_ORIGINS_ENV),
        )
        if value is None
    ]
    if missing:
        sys.stderr.write(
            "ERROR: refusing to start with insecure defaults -- "
            f"{' and '.join(missing)} not set. Set them before a real deployment, "
            "or set GEPER_DEV_INSECURE=1 to run insecurely on purpose (dev/test only).\n"
        )
        sys.exit(1)


_refuse_insecure_defaults_unless_opted_in()


async def _require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency: validate X-Api-Key header against GEPER_API_KEYS.

    If GEPER_API_KEYS is not set, allows all requests (dev mode).
    Raises HTTP 401 if an invalid or missing key is supplied in protected mode.
    """
    if _API_KEYS is None:
        # Dev mode — no auth required
        return
    if not x_api_key or x_api_key not in _API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Provide a valid key in the X-Api-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


app = FastAPI(title="Bij AI Structures API")

# CORS — restrict origins in production via GEPER_CORS_ORIGINS env var
_cors_origins = (_CORS_ORIGINS_ENV or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EntryStatus(str, enum.Enum):
    """
    Whether an AlphaFold DB structural entry exists for the queried
    accession at all -- the same shape as
    `pipeline/stage_schemas.py::StageStatus`, applied one layer up: "never
    queried" (NOT_RUN), "queried, don't know" (ERROR), "queried, confirmed
    absent" (NOT_FOUND), and "queried, exists" (FOUND) are four different
    claims, not one collapsed absence.
    """

    NOT_RUN = "not_run"
    ERROR = "error"
    NOT_FOUND = "not_found"
    FOUND = "found"


class MappingStatus(str, enum.Enum):
    """
    Only meaningful when `entry_status == FOUND` -- there is no residue to
    map onto a protein that was never found. `NO_POSITION` and
    `NOT_MAPPABLE` split "found, not mapped" by cause; see this module's own
    docstring for how the split is derived.
    """

    MAPPED = "mapped"
    NO_POSITION = "no_position"
    NOT_MAPPABLE = "not_mappable"


class InterpretationSubmissionRequest(BaseModel):
    """Request body for POST /interpretations."""

    submission_key: str
    vcf_path: str
    assembly: str
    sample_ref: str
    consent_ref: str
    hpo_terms: Optional[Dict[str, Any]] = None
    qc_metrics: Optional[Dict[str, Any]] = None


class InterpretationSubmissionResponse(BaseModel):
    """Response for POST /interpretations."""

    id: str
    status: str
    interpretation_id: Optional[str] = None


class InterpretationStatusResponse(BaseModel):
    """Response for GET /interpretations/{id}."""

    id: str
    status: str
    interpretation_id: Optional[str] = None
    error_message: Optional[str] = None


class StructureAnnotationResponse(BaseModel):
    """
    The wire contract for `GET /structures/{accession}`.

    `accession`, `pdb_url`, `mapped_residue`, `mapping_confidence_band`,
    and `model_version` are the five payload fields fixed by the human's
    ruling -- untouched here, no sixth data field added. Every other field
    is status/reason envelope around them, added because the ruling fixed
    the DATA shape, not the absence of a way to say why the data isn't
    there (see the design dispatch's requirement that the response "carry
    THE REASON").
    """

    entry_status: EntryStatus
    entry_status_reason: Optional[str] = None

    accession: Optional[str] = None
    pdb_url: Optional[str] = None
    model_version: Optional[str] = None

    mapping_status: Optional[MappingStatus] = None
    mapping_unavailable_reason: Optional[str] = None

    mapped_residue: Optional[int] = None
    mapping_confidence_band: Optional[str] = None


_lookup_singleton: Optional[AlphaFoldLookup] = None
_submission_store: Optional[SubmissionStore] = None


def _get_submission_store_path() -> Path:
    """Get path to submission store database.

    Uses GEPER_SUBMISSION_STORE_PATH env var, defaults to geper/.submissions.db
    """
    env_path = os.getenv("GEPER_SUBMISSION_STORE_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).parent / ".submissions.db"


def get_submission_store() -> SubmissionStore:
    """FastAPI dependency, overridable in tests via `app.dependency_overrides`."""
    global _submission_store
    if _submission_store is None:
        _submission_store = SubmissionStore(_get_submission_store_path())
    return _submission_store


def get_alphafold_lookup() -> AlphaFoldLookup:
    """FastAPI dependency, overridable in tests via `app.dependency_overrides`."""
    global _lookup_singleton
    if _lookup_singleton is None:
        _lookup_singleton = AlphaFoldLookup()
    return _lookup_singleton


def _map_to_response(raw: Dict[str, Any]) -> StructureAnnotationResponse:
    """
    Pure mapping from `AlphaFoldLookup.query_variant`'s raw dict to the
    ratified response shape. Every branch below is grounded in a specific,
    real code path in pipeline/alphafold/lookup.py and provider.py -- see
    tests/test_api_structures.py's fixtures, each copied from reading that
    source directly, not invented.
    """
    if raw.get("skipped"):
        # lookup.py's `_SKIPPED_RESULT` -- CONFIG.alphafold.ENABLED is
        # False. Returned before `accession` is computed, so it may be
        # absent from `raw` entirely; `.get` handles that as None.
        return StructureAnnotationResponse(
            entry_status=EntryStatus.NOT_RUN,
            entry_status_reason=raw.get("reason"),
            accession=raw.get("accession"),
        )

    if not raw.get("found"):
        error = raw.get("error")
        if error is not None:
            # AlphaFoldAnnotation.from_error(...).to_dict() -- the AlphaFold
            # DB query itself failed. "We don't know," distinct from
            # NOT_FOUND's "we know there's nothing."
            return StructureAnnotationResponse(
                entry_status=EntryStatus.ERROR,
                entry_status_reason=error,
                accession=raw.get("accession"),
            )

        reason = raw.get("reason")
        if reason is not None:
            # lookup.py's "no UniProt accession available" early return --
            # a second NOT_RUN cause (nothing to query), not an entry-level
            # error and not a 404. Checked after `error` deliberately:
            # AlphaFoldAnnotation.to_dict() never sets a "reason" key, so
            # this can only fire for the two NOT_RUN-shaped dicts.
            return StructureAnnotationResponse(
                entry_status=EntryStatus.NOT_RUN,
                entry_status_reason=reason,
                accession=raw.get("accession"),
            )

        # AlphaFoldAnnotation.not_found(...).to_dict() -- a clean 404.
        # "We know there's nothing," no reason needed beyond the status itself.
        return StructureAnnotationResponse(
            entry_status=EntryStatus.NOT_FOUND,
            accession=raw.get("accession"),
        )

    # found == True. Entry-level facts (pdb_url, model_version) are true of
    # the protein regardless of the residue-level outcome below -- see
    # provider.py::_build_annotation's own comment on this.
    accession = raw.get("accession")
    pdb_url = raw.get("pdb_url")
    model_version = raw.get("model_version")

    if raw.get("affected_residue_plddt") is not None:
        return StructureAnnotationResponse(
            entry_status=EntryStatus.FOUND,
            accession=accession,
            pdb_url=pdb_url,
            model_version=model_version,
            mapping_status=MappingStatus.MAPPED,
            mapped_residue=raw.get("protein_position"),
            mapping_confidence_band=raw.get("affected_residue_band"),
        )

    # Found, but the gate did not approve a residue-level value. Split by
    # cause using `raw`'s own `protein_position`: it is echoed
    # unconditionally by provider.py::_build_annotation regardless of the
    # gate's verdict (see this module's docstring), so None here means no
    # position was ever resolved, and non-None means a real position was
    # rejected on the merits. mapped_residue and mapping_confidence_band
    # stay None either way -- never populated from `protein_position` here,
    # even when it's present in `raw` (the position that was REJECTED, not
    # one that may be reported).
    mapping_status = MappingStatus.NO_POSITION if raw.get("protein_position") is None else MappingStatus.NOT_MAPPABLE
    return StructureAnnotationResponse(
        entry_status=EntryStatus.FOUND,
        accession=accession,
        pdb_url=pdb_url,
        model_version=model_version,
        mapping_status=mapping_status,
        mapping_unavailable_reason=raw.get("mapping_unavailable_reason"),
    )


@app.get(
    "/structures/{accession}",
    response_model=StructureAnnotationResponse,
    tags=["structures"],
    summary="AlphaFold DB structural annotation for one variant's mapped residue",
)
def get_structure_annotation(
    accession: str,
    protein_position: Optional[int] = None,
    lookup: AlphaFoldLookup = Depends(get_alphafold_lookup),
    _auth: None = Depends(_require_api_key),
) -> StructureAnnotationResponse:
    """
    `protein_position` is optional: omitting it is a valid request (the
    viewer wants the whole structure with no residue highlight) and is not
    an error -- it produces `mapping_status: no_position` with a reason
    naming exactly that.
    """
    raw = lookup.query_variant({"accession": accession}, protein_position=protein_position)
    return _map_to_response(raw)


@app.post(
    "/interpretations",
    response_model=InterpretationSubmissionResponse,
    status_code=202,
    tags=["interpretations"],
    summary="Submit a VCF for Bij AI interpretation",
)
def post_interpretation(
    req: InterpretationSubmissionRequest,
    store: SubmissionStore = Depends(get_submission_store),
    _auth: None = Depends(_require_api_key),
) -> InterpretationSubmissionResponse:
    """
    Submit a VCF for Bij AI interpretation.

    Returns:
    - 202 if new submission created (queued)
    - 200 if existing complete submission (idempotency)

    Client should not branch on status code — both responses include id and status.
    """
    submission = store.create_submission(
        org_id="default",
        submission_key=req.submission_key,
        vcf_path=req.vcf_path,
        assembly=req.assembly,
        sample_ref=req.sample_ref,
        consent_ref=req.consent_ref,
        hpo_terms=req.hpo_terms,
        qc_metrics=req.qc_metrics,
    )

    if submission.status == "complete":
        # Existing complete submission — return 200
        return InterpretationSubmissionResponse(
            id=submission.id,
            status=submission.status,
            interpretation_id=submission.interpretation_id,
        )

    # New or in-progress submission — return 202
    return InterpretationSubmissionResponse(
        id=submission.id,
        status=submission.status,
        interpretation_id=submission.interpretation_id,
    )


@app.get(
    "/interpretations/{submission_id}",
    response_model=InterpretationStatusResponse,
    tags=["interpretations"],
    summary="Get Bij AI interpretation status",
)
def get_interpretation_status(
    submission_id: str,
    store: SubmissionStore = Depends(get_submission_store),
    _auth: None = Depends(_require_api_key),
) -> InterpretationStatusResponse:
    """Get status of an interpretation submission."""
    submission = store.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    return InterpretationStatusResponse(
        id=submission.id,
        status=submission.status,
        interpretation_id=submission.interpretation_id,
        error_message=submission.error_message,
    )
