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
No auth, CORS, or health-check surface has been added beyond what that
endpoint itself needs to run -- those are open, separately-owned questions
(see the 2026-08-23 hive design/implementation dispatch), not decided here.

Architecture note (why this lives in geper/, not kim_pipeline/api/): ruled
2026-08-23 as Option A. kim_pipeline/api/main.py is the monorepo's only
other FastAPI app, but kim_pipeline has no structural code and geper/ and
kim_pipeline/ run as separate subprocesses with no shared Python process
(see bridge/combined_pipeline.py's own docstring) -- serving this from
kim_pipeline would mean either creating an in-process dependency that does
not exist, or geper persisting structural data for kim_pipeline to read,
which is what today's pass-through design avoids. This endpoint lives next
to the data, the mapping gate, and the provenance that produce it.

KNOWN GAP, carded and NOT resolved here: `mapping_status` collapses two
distinct causes of "found, but no residue-level confidence" --
(a) no protein position was ever resolved for this variant, and
(b) a real position existed but the mapping gate rejected it on the merits
(out of span / not modelled / fragment-numbering mismatch) --
into a single `NOT_MAPPED` value. The split this app's design called for
(NO_POSITION vs. NOT_MAPPABLE) is a carded open item pending a human
decision between: (a) pattern-matching `mapping_gate.py`'s reason string
(rejected -- turns a human-readable sentence into business logic a future
copy-edit could silently break), (b) extending `mapping_gate.py`'s own
contract to return a reason CODE alongside the prose (a real change to a
different module), or a third option surfaced during implementation --
deriving the split from whether THIS endpoint's own `protein_position`
query parameter was supplied at all, which needs no gate change and no
string-matching, since the raw result's own `protein_position` field is
just an echo of what this endpoint passed in. See the implementation
report for the full reasoning; this file deliberately does not decide it.
"""

from __future__ import annotations

import enum
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from pipeline.alphafold.lookup import AlphaFoldLookup

app = FastAPI(title="Bij AI Structures API")


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
    map onto a protein that was never found. `NOT_MAPPED` is a deliberately
    collapsed placeholder for two distinct causes; see this module's own
    docstring for why the split is not implemented yet.
    """

    MAPPED = "mapped"
    NOT_MAPPED = "not_mapped"


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

    # Found, but the gate did not approve a residue-level value (for
    # either reason this module's docstring names). mapped_residue and
    # mapping_confidence_band stay None -- never populated from
    # `protein_position` here, even when it's present in `raw` (the
    # position that was REJECTED, not one that may be reported).
    return StructureAnnotationResponse(
        entry_status=EntryStatus.FOUND,
        accession=accession,
        pdb_url=pdb_url,
        model_version=model_version,
        mapping_status=MappingStatus.NOT_MAPPED,
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
) -> StructureAnnotationResponse:
    """
    `protein_position` is optional: omitting it is a valid request (the
    viewer wants the whole structure with no residue highlight) and is not
    an error -- it produces `mapping_status: not_mapped` with a reason
    naming exactly that, same as any other unmapped case.
    """
    raw = lookup.query_variant({"accession": accession}, protein_position=protein_position)
    return _map_to_response(raw)
