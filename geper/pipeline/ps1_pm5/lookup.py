"""
ClinVarCodonLookup: the facade `pipeline/orchestrator.py` talks to for
PS1/PM5's shared evidence-gathering step -- every ClinVar record with a
parseable missense protein change at a given codon.

This is deliberately a *codon*-neighborhood search, not a
`database/clinvar_client.py::ClinVarClient.query_variant`-style
single-position lookup: PS1/PM5 both need every OTHER variant at the
same codon, which can differ from the query variant at any of the
codon's up to three genomic positions (and, for a "split codon"
straddling an exon-exon junction, those positions are not even
contiguous -- see `pipeline/pvs1/models.py::TranscriptContext.
genomic_positions_for_codon`'s docstring). Each position is queried
independently via NCBI's public E-utilities (the same `esearch`+
`esummary` shape `database/clinvar_client.py` already uses, with the
same retry/backoff/politeness conventions), and the results are merged
and de-duplicated by ClinVar UID.

Layering: ClinVarCodonLookup -> ClinVarCodonCache -> NCBI E-utilities.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.ps1_pm5.cache import ClinVarCodonCache
from pipeline.ps1_pm5.utils import clinvar_codon_match_from_esummary, dedupe_by_uid
from pipeline.pvs1.models import TranscriptContext
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.ncbi_eutils import lenient_json_loads, parse_retry_after, warn_if_placeholder_contact

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "PS1/PM5 ClinVar codon lookup disabled via GEPER_ENABLE_PS1_PM5=false",
    "found": False,
}


class ClinVarCodonLookup:
    """High-level, cached ClinVar codon-neighborhood lookup used by the orchestrator's PS1/PM5 stage."""

    def __init__(self, cache: Optional[ClinVarCodonCache] = None):
        self.cache = cache or (
            ClinVarCodonCache(
                max_size=CONFIG.ps1_pm5.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.ps1_pm5.CACHE_TTL_SECS,
                disk_path=CONFIG.ps1_pm5.CACHE_DISK_PATH or None,
            )
            if CONFIG.ps1_pm5.CACHE_ENABLED
            else None
        )
        warn_if_placeholder_contact(CONFIG.api.NCBI_EMAIL, caller="ClinVarCodonLookup")

    def query_codon(
        self, transcript: TranscriptContext, codon_number: int, assembly: str = "GRCh38"
    ) -> Dict[str, Any]:
        """
        Every ClinVar record with a parseable missense protein change
        found at any genomic position of `codon_number` in `transcript`.
        Never raises: a network failure is reported inside the returned
        dict's `error` field, matching every other lookup in this
        codebase.
        """
        if not CONFIG.ps1_pm5.ENABLED:
            return dict(_SKIPPED_RESULT)

        positions = transcript.genomic_positions_for_codon(codon_number)
        if not positions:
            return {
                "skipped": False, "found": False, "matches": [],
                "reason": f"codon {codon_number} is out of range for transcript {transcript.transcript_id}.",
            }

        key = f"codon:{assembly}:{transcript.transcript_id}:{codon_number}"
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        chrom = transcript.chrom
        position_field = "chrpos37" if str(assembly).upper().startswith("GRCH37") else "chrpos38"

        all_matches = []
        errors = []
        for pos in positions:
            try:
                uids = self._esearch(f"{chrom}[chr] AND {pos}[{position_field}]")
                if uids:
                    all_matches.extend(self._esummary(uids))
            except ExternalAPIError as exc:
                errors.append(str(exc))
                logger.warning(f"ClinVar codon lookup failed for {transcript.transcript_id} codon {codon_number} @ {chrom}:{pos}: {exc}")

        matches = dedupe_by_uid(all_matches)
        # `matches` is the plain-dict form throughout -- the same
        # shape whether this result was just fetched or came back from
        # `self.cache` a moment ago, so `pipeline/ps1_pm5/decision.py`
        # never needs to know which. (An earlier version of this
        # method also carried the typed `ClinVarCodonMatch` objects
        # alongside the dicts for the fresh-fetch path only; that was
        # a real bug in waiting, since a cache hit -- the common case
        # for a hotspot codon queried by several variants in one run --
        # would have silently produced a result decision.py couldn't
        # read the same way as a fresh one.)
        result = {
            "skipped": False,
            "found": bool(matches),
            "source": "ncbi_eutils",
            "transcript_id": transcript.transcript_id,
            "codon_number": codon_number,
            "queried_positions": positions,
            "matches": [m.to_dict() for m in matches],
            "error": "; ".join(errors) if errors and not matches else None,
        }
        if self.cache is not None and not (errors and not matches):
            # Cache a clean result, or a partial one that still found
            # something; never cache a total failure, so a transient
            # NCBI outage doesn't paper over every codon for the TTL.
            self.cache.put(key, dict(result))
        return result

    # ------------------------------------------------------------------
    # NCBI E-utilities (mirrors database/clinvar_client.py's shape)
    # ------------------------------------------------------------------

    def _esearch(self, term: str) -> List[str]:
        params = {
            "db": CONFIG.api.CLINVAR_DB,
            "term": term,
            "retmode": "json",
            "retmax": 50,
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        payload = self._request_json(f"{CONFIG.api.NCBI_EUTILS_BASE}/esearch.fcgi", params)
        return payload.get("esearchresult", {}).get("idlist", [])

    def _esummary(self, uids: List[str]):
        params = {
            "db": CONFIG.api.CLINVAR_DB,
            "id": ",".join(uids),
            "retmode": "json",
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        payload = self._request_json(f"{CONFIG.api.NCBI_EUTILS_BASE}/esummary.fcgi", params)
        result = payload.get("result", {})
        matches = []
        for uid in result.get("uids", []):
            match = clinvar_codon_match_from_esummary(uid, result.get(uid, {}))
            if match is not None:
                matches.append(match)
        return matches

    def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        GET `url` and return the parsed JSON body, retrying up to
        `CONFIG.ps1_pm5.MAX_RETRIES` times. Mirrors
        `database/clinvar_client.py::_request_json`'s two fixes: a 429
        honors NCBI's `Retry-After` header instead of guessing via the
        default exponential backoff, and the JSON body is parsed
        leniently (`lenient_json_loads`) so a raw control character
        inside a string value (observed live in a real ClinVar
        response) is recovered as the complete, valid data it is,
        rather than exhausting every retry attempt on a response that
        was never actually a network failure.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.ps1_pm5.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.ps1_pm5.QUERY_TIMEOUT_SECS)
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    wait = retry_after if retry_after is not None else CONFIG.ps1_pm5.RETRY_BACKOFF_SECS * attempt
                    last_error = requests.HTTPError(f"429 Too Many Requests from '{url}'")
                    logger.warning(
                        f"PS1/PM5 ClinVar request attempt {attempt} rate-limited (429); waiting {wait:.1f}s "
                        f"({'NCBI Retry-After header' if retry_after is not None else 'default backoff'})."
                    )
                    if attempt < CONFIG.ps1_pm5.MAX_RETRIES:
                        time.sleep(wait)
                    continue
                response.raise_for_status()
                return lenient_json_loads(response.text, source=url)
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"PS1/PM5 ClinVar request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.ps1_pm5.MAX_RETRIES:
                    time.sleep(CONFIG.ps1_pm5.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(f"PS1/PM5 ClinVar request to '{url}' failed after {CONFIG.ps1_pm5.MAX_RETRIES} attempts: {last_error}")
