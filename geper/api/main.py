"""
api/main.py
────────────
Bij AI variant-interpretation component (GEPER) structural-annotation FastAPI application.

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
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from component_identity import COMPONENT_NAME, SHORT_NAME
from pipeline.alphafold.lookup import AlphaFoldLookup

from .submission_store import SubmissionStore

logger = logging.getLogger("geper.structures_api")

# ─── API key authentication (ported from kim_pipeline/api/main.py's fail-to-
# start control -- see that module's GAP 2 / FIX #3 for the original) ────────


def _parse_api_keys(raw: str) -> Optional[Dict[str, uuid.UUID]]:
    """Parse GEPER_API_KEYS: comma-separated `key:org_uuid` entries.

    2026-09-12 (D0, "build the link"): every key is bound to ONE clinical
    organisation, and a submission belongs to the organisation of the key
    that made it. Before this the value was a bare key list and every
    submission was filed under the literal string "default" -- which is not
    an organisation, and which the worker's exception path then crashed
    parsing as a UUID. The organisation is deliberately NOT a request field:
    any key holder could then write into any tenant.

    Returns None when nothing is configured (dev mode). Raises ValueError on
    any malformed entry -- a bare key (the old format), an empty key, an
    organisation that is not a UUID, or one key bound to two organisations
    -- rather than dropping the entry or binding it to nothing. The message
    never contains a key.
    """
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    if not entries:
        return None
    keys: Dict[str, uuid.UUID] = {}
    for position, entry in enumerate(entries, start=1):
        key, sep, org = entry.partition(":")
        key, org = key.strip(), org.strip()
        if not sep or not key or not org:
            raise ValueError(f"GEPER_API_KEYS entry {position} is not in the form key:org_uuid")
        try:
            org_id = uuid.UUID(org)
        except ValueError:
            raise ValueError(f"GEPER_API_KEYS entry {position}: the organisation is not a UUID") from None
        if key in keys and keys[key] != org_id:
            raise ValueError(f"GEPER_API_KEYS entry {position}: this key is already bound to another organisation")
        keys[key] = org_id
    return keys


def _load_api_keys() -> Optional[Dict[str, uuid.UUID]]:
    """Load GEPER_API_KEYS; refuse to start on a malformed value.

    Returns None if not set (dev mode -- the structures endpoint allows all
    requests; submissions are refused, since there is no organisation to own
    them).
    """
    try:
        return _parse_api_keys(os.getenv("GEPER_API_KEYS", ""))
    except ValueError as exc:
        sys.stderr.write(
            f"ERROR: refusing to start -- {exc}. Each GEPER_API_KEYS entry must be "
            "key:org_uuid, binding the key to the clinical organisation its submissions "
            "belong to.\n"
        )
        sys.exit(1)


_API_KEYS: Optional[Dict[str, uuid.UUID]] = _load_api_keys()
_CORS_ORIGINS_ENV: Optional[str] = os.getenv("GEPER_CORS_ORIGINS")  # None if unset; "*" applied later
_DEV_INSECURE: bool = os.getenv("GEPER_DEV_INSECURE", "") == "1"

if _API_KEYS is None:
    logger.warning(
        "GEPER_API_KEYS is not set — running in dev mode with no authentication. "
        "Set GEPER_API_KEYS=key1:org_uuid1,key2:org_uuid2 before deploying to production."
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


async def _require_organisation(x_api_key: str = Header(default="")) -> uuid.UUID:
    """FastAPI dependency for the /interpretations endpoints: the clinical
    organisation the presented key is bound to.

    Unlike `_require_api_key`, dev mode does NOT pass: with no keys
    configured there is no organisation, and a submission must belong to
    one (it becomes an org-scoped clinical record). The old code filed such
    submissions under the string "default".
    """
    if _API_KEYS is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Interpretation submissions need an API key bound to a clinical organisation "
                "(GEPER_API_KEYS=key:org_uuid); none are configured, so there is no organisation "
                "to own this submission."
            ),
            headers={"WWW-Authenticate": "ApiKey"},
        )
    org_id = _API_KEYS.get(x_api_key) if x_api_key else None
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Provide a valid key in the X-Api-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return org_id


# Scoped 2026-09-12: this app serves only GEPER (AlphaFold structure lookups,
# and VCF submissions that api/submission_worker.py runs through geper/main.py).
app = FastAPI(title=f"{COMPONENT_NAME} Structures API")

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
    # The clinical order and sample this run interprets (2026-09-12, D0).
    # Required: the worker writes the result as a clinical interpretation of
    # this sample, and a run it cannot attribute to one is not run. The
    # organisation is NOT here -- it comes from the API key.
    order_id: uuid.UUID
    sample_id: uuid.UUID
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
    summary=f"Submit a VCF for interpretation by the {COMPONENT_NAME}",
)
def post_interpretation(
    req: InterpretationSubmissionRequest,
    response: Response,
    store: SubmissionStore = Depends(get_submission_store),
    org_id: uuid.UUID = Depends(_require_organisation),
) -> InterpretationSubmissionResponse:
    """
    Submit a VCF for interpretation by the Bij AI variant-interpretation component (GEPER).

    Returns:
    - 202 when this request CREATED the submission (queued for the worker)
    - 200 when this request REPLAYED one: this organisation's `submission_key`
      was already on file, and the body is the submission that already exists

    Fixed 2026-09-13 (wave 117). Both branches used to answer 202 -- the
    "existing complete submission" branch below set no status code of its own,
    so the route's `status_code=202` stood -- and this docstring told clients
    "should not branch on status code", which conceded the distinction had
    been collapsed rather than intending it. 202 Accepted means "I have taken
    this and not yet acted on it"; answering it to a submission that is
    already complete states something false about what the server just did.
    A client that retries after a timeout can now learn whether its first
    attempt landed, without a second GET.

    The body shape is unchanged: both branches return the same
    `InterpretationSubmissionResponse`, and `status` remains the field that
    says where the run has got to. A client that reads only the body behaves
    exactly as before.

    Not a replay: another organisation sending the same `submission_key`. The
    idempotency key is UNIQUE(org_id, submission_key), so that request creates
    its own submission and gets its own 202.
    """
    submission, replayed = store.create_or_replay_submission(
        org_id=str(org_id),
        order_id=str(req.order_id),
        sample_id=str(req.sample_id),
        submission_key=req.submission_key,
        vcf_path=req.vcf_path,
        assembly=req.assembly,
        sample_ref=req.sample_ref,
        consent_ref=req.consent_ref,
        hpo_terms=req.hpo_terms,
        qc_metrics=req.qc_metrics,
    )

    if replayed:
        # Already on file for this organisation: nothing was accepted here, so
        # 200, not 202. Decided by the store inside the same transaction as the
        # existence check, not by re-reading `submission.status` -- a replayed
        # submission that is still `queued` or `running` is just as much a
        # replay as a `complete` one, which the old status-based branch missed.
        response.status_code = 200

    return InterpretationSubmissionResponse(
        id=submission.id,
        status=submission.status,
        interpretation_id=submission.interpretation_id,
    )


@app.get(
    "/interpretations/{submission_id}",
    response_model=InterpretationStatusResponse,
    tags=["interpretations"],
    summary=f"Get {SHORT_NAME} interpretation status",
)
def get_interpretation_status(
    submission_id: str,
    store: SubmissionStore = Depends(get_submission_store),
    org_id: uuid.UUID = Depends(_require_organisation),
) -> InterpretationStatusResponse:
    """Get status of an interpretation submission.

    Org-scoped: another organisation's submission answers 404, the same as
    an id that does not exist, so a key cannot probe other tenants' ids.
    """
    submission = store.get_submission(submission_id)
    if not submission or submission.org_id != str(org_id):
        raise HTTPException(status_code=404, detail="Submission not found")

    return InterpretationStatusResponse(
        id=submission.id,
        status=submission.status,
        interpretation_id=submission.interpretation_id,
        error_message=submission.error_message,
    )
