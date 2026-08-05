"""
IndiGenomes population-frequency evidence -- a live query integration
against CSIR-IGIB's public IndiGenomes resource
(https://clingen.igib.res.in/indigen/), built from over 1000 Indian
genomes (Jain A. et al. 2020, "IndiGenomes: a comprehensive resource
of genetic variants from over 1000 Indian genomes", Nucleic Acids
Research, doi: 10.1093/nar/gkaa923, PMID 33095885). The resource's own
UI states it "is intended for purely research purposes" -- noted here
as a citation of the source's own disclaimer, not a GEPER compliance
claim.

Design note -- why this is a live query, not a downloaded local index
=======================================================================
The original design for this integration assumed IndiGenomes ships a
downloadable, per-variant allele-frequency file, the same way gnomAD
publishes bgzip'd, tabix-indexed site VCFs (see
`pipeline/gnomad/provider.py::LocalIndexedGnomadProvider`). That
assumption was checked against the live site before any code was
written, and turned out to be false in two independent ways:

  1. The one bulk file the site does publish
     ("Download Indigen Alu final geno10 all 22K vcf file" is a
     different, Alu-specific dataset; the actual whole-genome file is
     linked from the plain "Download" button:
     https://clingen.igib.res.in/indigen/download/IndiGenomes_Variants.vcf.gz,
     confirmed live 2026-08-06: HTTP 200, ~137MB, `Content-Type:
     application/x-gzip`) is **plain gzip, not BGZF** (its magic bytes
     are `1f 8b 08 08`, not BGZF's `1f 8b 08 04` + "BC" extra field),
     so it isn't directly tabix-indexable either way. More importantly,
     its records carry only `VRT=` (variant type); there is no AC/AF/AN
     INFO field anywhere in it. There is nothing to index for a
     frequency lookup.
  2. The only place IndiGenomes actually exposes AC/AF/AN is a
     per-variant JSON search endpoint its own AngularJS frontend calls
     (`POST https://clingen.igib.res.in/indigen/data.php`, request body
     `{"Name": "<chrN-POS-REF-ALT | chrN:POS:REF:ALT | rsID | gene>"}`)
     -- confirmed live against several known variants, INCLUDING a
     deliberately non-exonic/intergenic position (chr1-10190-C-A) to
     verify genome-wide, not just coding-region, coverage. Each hit's
     `"Info"` field carries the cohort's own site-level
     `AC=..;AF=..;AN=..;...` string (e.g.
     `"AC=1;AF=0.000;AN=2048;DP=26929;..."` for a real MVK missense
     variant) -- this is the actual, and only, source of IndiGenomes
     allele-frequency evidence.

So this module queries that live endpoint directly, per variant --
the same "query a live API, retry/backoff/timeout, never raise"
contract `pipeline/gnomad/provider.py::GraphQLGnomadProvider` already
established for gnomAD's own API fallback, just without a local-index
option, since IndiGenomes never publishes one usable for this purpose.
An in-process cache (`_CACHE`, in-memory only -- no disk tier, unlike
`pipeline/gnomad/cache.py::GnomadCache`) avoids re-querying the same
variant twice within one run; this is a small public research server,
not a production API with its own caching/CDN, and deserves to be
treated politely.

GRCh38 only: IndiGenomes' own VCF header declares
`##reference=GCF_000001405.39` (GRCh38.p13) with no GRCh37/hg19
release ever published. `IndiGenomesLookup.query_variant` refuses to
query a run resolved to GRCh37 rather than risk a coordinate collision
against the wrong build silently misreporting an allele frequency for
a genomically different position.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional, cast

import requests

from config import CONFIG
from pipeline.gnomad.utils import normalize_build
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)

_INFO_KV_RE = re.compile(r"(\w[\w.\-]*)=([^;]*)")

# In-process only, unbounded eviction policy ("clear on overflow") --
# see this module's docstring for why a full LRU isn't warranted here.
_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _cache_key(chrom: str, pos: int, ref: str, alt: str) -> str:
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return f"{bare}:{pos}:{ref.upper()}:{alt.upper()}"


def _cache_get(key: str) -> Any:
    return _CACHE.get(key, _MISSING)


_MISSING = object()


def _cache_put(key: str, value: Optional[Dict[str, Any]]) -> None:
    if len(_CACHE) >= CONFIG.indigenomes.CACHE_MAX_SIZE:
        _CACHE.clear()
    _CACHE[key] = value


def _parse_info_af_ac_an(info: str) -> Dict[str, Optional[float]]:
    """
    Parse IndiGenomes' own cohort-level VCF INFO string
    (`AC=1;AF=0.000;AN=2048;DP=26929;...`, observed live in the
    `data.php` response's `"Info"` field) into `{"af", "ac", "an"}`.

    Deliberately not `pipeline/gnomad/utils.py::parse_vcf_info_field`
    (which returns the whole dict of raw string values) -- this module
    has no dependency on `pipeline.gnomad` beyond `normalize_build`
    (IndiGenomes is an independent data source, not a gnomAD release),
    so its own tiny INFO-string parser is self-contained here instead.
    """
    fields = dict(_INFO_KV_RE.findall(info or ""))

    def _as_float(key: str) -> Optional[float]:
        try:
            return float(fields[key])
        except (KeyError, ValueError):
            return None

    def _as_int(key: str) -> Optional[int]:
        try:
            return int(float(fields[key]))
        except (KeyError, ValueError):
            return None

    return {"af": _as_float("AF"), "ac": _as_int("AC"), "an": _as_int("AN")}


def _search_name(chrom: str, pos: int, ref: str, alt: str) -> str:
    """IndiGenomes' own `chrN-POS-REF-ALT` search-box variant format (confirmed live via the site's UI: the "Example Search" box shows `chr12-109586107-A-G`)."""
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return f"chr{bare}-{pos}-{ref.upper()}-{alt.upper()}"


def _post(name: str) -> Dict[str, Any]:
    """
    POST one search to IndiGenomes' `data.php` endpoint. Retry/backoff/
    timeout mirrors `pipeline/gnomad/provider.py::GraphQLGnomadProvider
    ._post` -- same shape every external-API client in this codebase
    uses. Body is sent as a raw JSON string, matching the site's own
    frontend exactly (confirmed live via its `angularHtml.js`: it sets
    `Content-Type: application/x-www-form-urlencoded` but still sends
    `JSON.stringify({"Name": ...})` as the body -- an inconsistency in
    the site's own code, not this module's; replicated here because
    the server evidently expects it, confirmed by a direct `curl`
    reproduction).
    """
    last_error: Optional[Exception] = None
    body = json.dumps({"Name": name})
    endpoint = CONFIG.indigenomes.ENDPOINT
    for attempt in range(1, CONFIG.indigenomes.MAX_RETRIES + 1):
        try:
            response = requests.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=CONFIG.indigenomes.QUERY_TIMEOUT_SECS,
            )
            response.raise_for_status()
            return cast(Dict[str, Any], response.json())
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(f"IndiGenomes request attempt {attempt} failed: {exc}")
            if attempt < CONFIG.indigenomes.MAX_RETRIES:
                time.sleep(CONFIG.indigenomes.RETRY_BACKOFF_SECS * attempt)
    raise ExternalAPIError(
        f"IndiGenomes request to '{endpoint}' failed after {CONFIG.indigenomes.MAX_RETRIES} attempts: {last_error}"
    )


def _lookup(chrom: str, pos: int, ref: str, alt: str) -> Dict[str, Any]:
    """
    Shared implementation for both `query_indigenomes` and
    `IndiGenomesLookup.query_variant` below. Always returns a dict with
    a `"status"` key -- `"found"` (with `"af"`/`"ac"`/`"an"`),
    `"not_found"`, or `"error"` (with `"error"` set) -- so callers that
    need to distinguish those three states can (`IndiGenomesLookup`
    does; the plain `query_indigenomes()` function collapses all but
    `"found"` to `None`, per its documented, simpler contract).

    Allele-aware matching discipline, never position alone: matched on
    the response's own `Ref_VCF`/`Alt_VCF`/`Start_VCF` fields (the
    exact VCF-record values IndiGenomes itself resolved this hit to),
    not the search term echoed back -- mirroring
    `database/clinvar_client.py`/`database/dbsnp_client.py`'s own
    "position AND allele, never position alone" discipline (see those
    modules' docstrings). `data.php`'s search-by-position-and-allele
    query was confirmed live to return exactly the one matching allele
    at a locus, never a merged/ambiguous record, but this still
    verifies the response's own fields rather than trusting that by
    construction -- the same defense-in-depth those two clients apply
    to their own exact-match APIs.

    Note the real API's own key has a trailing space --
    `"Start_VCF "`, not `"Start_VCF"` -- observed live in the raw JSON
    response; `"Start"` (no such typo) is used instead, since it's the
    more reliable of the two duplicate position fields the response
    actually carries.
    """
    key = _cache_key(chrom, pos, ref, alt)
    cached = _cache_get(key)
    if cached is not _MISSING:
        return {"status": "found", **cached} if cached is not None else {"status": "not_found"}

    name = _search_name(chrom, pos, ref, alt)
    try:
        payload = _post(name)
    except ExternalAPIError as exc:
        logger.warning(f"IndiGenomes query failed for {name}: {exc}")
        return {"status": "error", "error": str(exc)}

    records = payload.get("mydata") or []
    match = None
    for record in records:
        if (
            str(record.get("Ref_VCF") or "").upper() == ref.upper()
            and str(record.get("Alt_VCF") or "").upper() == alt.upper()
            and int(record.get("Start") or -1) == pos
        ):
            match = record
            break

    if match is None:
        _cache_put(key, None)
        return {"status": "not_found"}

    result = _parse_info_af_ac_an(match.get("Info") or "")
    _cache_put(key, result)
    return {"status": "found", **result}


def query_indigenomes(chrom: str, pos: int, ref: str, alt: str) -> Optional[Dict[str, Any]]:
    """
    Query IndiGenomes' live per-variant endpoint for this exact
    chrom/pos/ref/alt.

    Returns `{"af": float | None, "ac": int | None, "an": int | None}`
    when a matching record was found (any of the three individually
    `None` only if IndiGenomes' own INFO string genuinely omitted it),
    or `None` when the variant is genuinely absent from IndiGenomes,
    the integration is disabled (`CONFIG.indigenomes.ENABLED = False`),
    or the live query itself failed after retries (network error,
    malformed response). This function intentionally does not
    distinguish those three `None` cases from one another -- matching
    the plain `{af, ac, an} | None` contract this was scoped to
    deliver. Callers that need to tell "not found" apart from "query
    failed" (the orchestrator's stage runner, provenance capture) use
    `IndiGenomesLookup.query_variant` below instead, which does.
    """
    if not CONFIG.indigenomes.ENABLED:
        return None
    outcome = _lookup(chrom, pos, ref, alt)
    if outcome["status"] != "found":
        return None
    return {"af": outcome.get("af"), "ac": outcome.get("ac"), "an": outcome.get("an")}


class IndiGenomesLookup:
    """
    Orchestrator-facing facade: one `query_variant(variant,
    assembly=...)` call, never raises, matches
    `pipeline.gnomad.lookup.GnomadLookup`/`database.clinvar_client
    .ClinVarClient`'s contract exactly so `pipeline/orchestrator.py`'s
    per-stage try/except wrapper only needs to guard against a
    genuinely unexpected bug here, not against this raising under
    normal operation.
    """

    def query_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        if not CONFIG.indigenomes.ENABLED:
            return {
                "skipped": True,
                "reason": "IndiGenomes integration disabled via GEPER_ENABLE_INDIGENOMES=false",
                "found": False,
            }

        build = normalize_build(assembly)
        if build != "GRCh38":
            return {
                "skipped": True,
                "reason": f"IndiGenomes is GRCh38-only; this run's assembly resolved to '{build}'.",
                "found": False,
            }

        outcome = _lookup(variant.chrom, variant.pos, variant.ref, variant.alt)
        if outcome["status"] == "error":
            return {"skipped": False, "found": False, "error": outcome["error"]}
        if outcome["status"] == "not_found":
            return {"skipped": False, "found": False}
        return {
            "skipped": False,
            "found": True,
            "af": outcome.get("af"),
            "ac": outcome.get("ac"),
            "an": outcome.get("an"),
        }
