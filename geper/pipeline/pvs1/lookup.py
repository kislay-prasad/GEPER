"""
TranscriptLookup: the facade `pipeline/orchestrator.py` talks to for the
transcript structure PVS1 needs, in the same shape as
`pipeline.clingen.lookup.ClinGenLookup` -- one `query_variant(variant,
assembly=..., gene_symbol=...)` call that returns a plain dict, never
raises, and is safe to call once per variant inside the main per-variant
loop.

Like ClinGen's, this lookup is *gene*-keyed rather than
coordinate-keyed: the structure fetched is a whole transcript, so a VCF
covering many variants in one gene pays the Ensembl round trip once.
The gene symbol itself is not resolved here -- the ClinGen stage already
resolves it (`pipeline/clingen/utils.py::resolve_gene_symbol`) and the
orchestrator passes it through, so a second identical Ensembl
gene-overlap call is avoided.

Source: Ensembl REST `lookup/symbol/{species}/{gene}?expand=1`, which
returns every transcript of the gene with its exon list and translation
block, and flags exactly one as `is_canonical`. That canonical
protein-coding transcript is the one used -- GENCODE's canonical
selection is MANE Select wherever a MANE transcript exists, which is the
transcript ACMG/AMP interpretation is normally performed against.

Layering: TranscriptLookup -> TranscriptCache -> Ensembl REST.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.ensembl.provider import transcript_for_gene
from pipeline.pvs1.cache import TranscriptCache
from pipeline.pvs1.utils import transcript_context_from_ensembl
from pipeline.vcf_parser import Variant
from utils.logger import get_logger
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "PVS1 transcript-structure lookup disabled via GEPER_ENABLE_PVS1=false",
    "found": False,
}

# Ensembl serves GRCh37 from a separate host rather than via a query
# parameter. Unlike gene *symbols* (see `pipeline/clingen/utils.py`,
# which deliberately accepts the GRCh38 mirror for both builds), exon
# coordinates genuinely differ between builds, so using the wrong mirror
# here would silently mis-place every splice site. The build is
# therefore routed explicitly.
_GRCH37_REST_BASE = "https://grch37.rest.ensembl.org"
_DEFAULT_REST_BASE = "https://rest.ensembl.org"


class TranscriptLookup:
    """High-level, cached transcript-structure lookup used by the orchestrator's PVS1 stage."""

    def __init__(self, cache: Optional[TranscriptCache] = None):
        self.cache = cache or (
            TranscriptCache(
                max_size=CONFIG.pvs1.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.pvs1.CACHE_TTL_SECS,
                disk_path=CONFIG.pvs1.CACHE_DISK_PATH or None,
            )
            if CONFIG.pvs1.CACHE_ENABLED
            else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query_variant(
        self,
        variant: Variant,
        assembly: str = "GRCh38",
        gene_symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transcript structure for the gene this variant falls in. The gene
        symbol must be supplied (the ClinGen stage resolves it); without
        one this reports "not found" rather than issuing a second
        gene-resolution call.
        """
        if not CONFIG.pvs1.ENABLED:
            return dict(_SKIPPED_RESULT)
        if not gene_symbol:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "transcript": None,
                "reason": "no gene symbol was resolved for this variant, so no transcript could be fetched.",
            }

        result = self.query_gene(gene_symbol, build=assembly)

        # A gene's canonical transcript does not necessarily span every
        # variant attributed to the gene (alternative first/last exons,
        # overlapping genes). Flagging that here keeps the decision tree
        # from silently evaluating a variant against a transcript that
        # does not contain it.
        transcript = result.get("transcript")
        if transcript and variant is not None:
            start, end = transcript.get("transcript_span") or (None, None)
            # `end is not None` guarded separately from `start` (G1,
            # report review round 5): the two were previously checked
            # asymmetrically -- a malformed `transcript_span` of
            # `(start, None)` would have passed the old `start is not
            # None` guard and then raised a real `TypeError` comparing
            # `variant.pos <= None` below, not just failed a mypy check.
            if start is not None and end is not None and not (start <= variant.pos <= end):
                result = dict(result)
                result["variant_outside_transcript"] = True
                result["reason"] = (
                    f"variant position {variant.pos} lies outside the canonical transcript "
                    f"{transcript.get('transcript_id')} ({start}-{end})."
                )
        return result

    def query_gene(self, gene_symbol: str, build: str = "GRCh38") -> Dict[str, Any]:
        """Transcript structure for a gene symbol directly (used by callers that already know the gene, e.g. tests/reports)."""
        if not CONFIG.pvs1.ENABLED:
            return dict(_SKIPPED_RESULT)

        gene_symbol = (gene_symbol or "").strip().upper()
        if not gene_symbol:
            return {"skipped": False, "found": False, "error": "no gene symbol provided", "transcript": None}

        key = f"transcript:{build}:{gene_symbol}"
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        result = self._fetch(gene_symbol, build)
        if self.cache is not None and not result.get("error"):
            self.cache.put(key, result)
        return result

    # ------------------------------------------------------------------
    # Ensembl
    # ------------------------------------------------------------------

    @staticmethod
    def _rest_base(build: str) -> str:
        configured = CONFIG.api.ENSEMBL_REST_BASE
        if str(build).upper().startswith("GRCH37") and configured.rstrip("/") == _DEFAULT_REST_BASE:
            return _GRCH37_REST_BASE
        return configured

    def _fetch(self, gene_symbol: str, build: str) -> Dict[str, Any]:
        if CONFIG.pvs1.OFFLINE_MODE:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": gene_symbol,
                "transcript": None,
                "reason": "PVS1 transcript lookup is in offline mode; no transcript structure fetched.",
            }

        local_result = self._fetch_local(gene_symbol, build)
        if local_result is not None:
            return local_result

        return self._fetch_live(gene_symbol, build)

    def _fetch_local(self, gene_symbol: str, build: str) -> Optional[Dict[str, Any]]:
        """
        Primary source: GEPER's self-provisioned Ensembl GTF+CDS cache
        (`pipeline/ensembl/`), tried before any network call. GRCh38
        only -- see `config.py::EnsemblConfig`'s docstring for why the
        cache doesn't cover GRCh37; a GRCh37 request skips straight to
        `_fetch_live`, same as an unavailable cache or a gene the cache
        has no entry for.

        Returns None -- never a guess -- whenever the cache can't
        answer, so `_fetch` falls through to the existing live Ensembl
        REST path exactly as it always has.
        """
        if str(build).upper().startswith("GRCH37"):
            return None

        # Calls the module-level `transcript_for_gene` imported above
        # (not a locally-scoped import) specifically so tests can patch
        # `pipeline.pvs1.lookup.transcript_for_gene` directly, the same
        # seam `pipeline.clingen.utils.mane_select_transcript_id`
        # already establishes for its own module-level provider import.
        transcript_dict = transcript_for_gene(gene_symbol)
        if transcript_dict is None:
            return None

        transcript_dict = dict(transcript_dict)
        exons = transcript_dict.get("exons") or []
        transcript_dict["transcript_span"] = (
            [min(e["start"] for e in exons), max(e["end"] for e in exons)] if exons else [None, None]
        )
        return {
            "skipped": False,
            "found": True,
            "gene_symbol": gene_symbol,
            "assembly": build,
            "source": "ensembl_gtf_cache",
            "transcript": transcript_dict,
        }

    def _fetch_live(self, gene_symbol: str, build: str) -> Dict[str, Any]:
        url = f"{self._rest_base(build).rstrip('/')}/lookup/symbol/homo_sapiens/{gene_symbol}"
        params = {"expand": "1", "content-type": "application/json"}

        if HEALTH.is_offline("Ensembl"):
            HEALTH.note_skip("Ensembl")
            message = f"Ensembl transcript lookup for {gene_symbol} skipped: Ensembl was confirmed offline at startup."
            return {"skipped": False, "found": False, "gene_symbol": gene_symbol, "transcript": None, "error": message}

        payload = None
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.pvs1.MAX_RETRIES + 1):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={"Content-Type": "application/json"},
                    timeout=CONFIG.pvs1.QUERY_TIMEOUT_SECS,
                )
                if response.status_code == 400:
                    # Ensembl answers an unknown symbol with 400, which is
                    # a definitive "no such gene", not a transient failure.
                    HEALTH.note_success("Ensembl")
                    return {
                        "skipped": False,
                        "found": False,
                        "gene_symbol": gene_symbol,
                        "transcript": None,
                        "reason": f"Ensembl has no gene with symbol '{gene_symbol}' on {build}.",
                    }
                response.raise_for_status()
                payload = response.json()
                HEALTH.note_success("Ensembl")
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"Ensembl transcript lookup attempt {attempt} failed for {gene_symbol}: {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.pvs1.MAX_RETRIES:
                    time.sleep(CONFIG.pvs1.RETRY_BACKOFF_SECS * attempt)

        if payload is None:
            HEALTH.note_failure("Ensembl")
            message = f"Ensembl transcript lookup failed for {gene_symbol} after {CONFIG.pvs1.MAX_RETRIES} attempts: {last_error}"
            logger.warning(message)
            return {"skipped": False, "found": False, "gene_symbol": gene_symbol, "transcript": None, "error": message}

        chosen = self._choose_transcript(payload.get("Transcript") or [])
        if chosen is None:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": gene_symbol,
                "transcript": None,
                "reason": f"no protein-coding transcript with a translation was returned for {gene_symbol}.",
            }

        cds_sequence = self._fetch_cds_sequence(chosen.get("id"), build)
        context = transcript_context_from_ensembl(
            chosen, gene_symbol=gene_symbol, source="ensembl_api", cds_sequence=cds_sequence
        )
        if context is None:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": gene_symbol,
                "transcript": None,
                "reason": f"Ensembl returned a transcript for {gene_symbol} without a usable exon/translation structure.",
            }

        transcript_dict = context.to_dict()
        transcript_dict["transcript_span"] = [chosen.get("start"), chosen.get("end")]
        return {
            "skipped": False,
            "found": True,
            "gene_symbol": gene_symbol,
            "assembly": build,
            "source": "ensembl_api",
            "transcript": transcript_dict,
        }

    def _fetch_cds_sequence(self, transcript_id: Optional[str], build: str) -> Optional[str]:
        """
        The transcript's CDS, in transcript orientation. Needed to call a
        substitution's consequence in the true reading frame rather than
        from a frame-unaware sequence window (see
        `pipeline/pvs1/utils.py::coding_consequence`).

        Best-effort by design: a failure here degrades PVS1 to the
        protein-window fallback for substitutions rather than failing the
        lookup, since every location caveat still works without it.
        """
        if not transcript_id or CONFIG.pvs1.OFFLINE_MODE:
            return None
        if HEALTH.is_offline("Ensembl"):
            HEALTH.note_skip("Ensembl")
            return None
        url = f"{self._rest_base(build).rstrip('/')}/sequence/id/{transcript_id}"
        try:
            response = requests.get(
                url,
                params={"type": "cds", "content-type": "application/json"},
                headers={"Content-Type": "application/json"},
                timeout=CONFIG.pvs1.QUERY_TIMEOUT_SECS,
            )
            response.raise_for_status()
            sequence = (response.json() or {}).get("seq")
            HEALTH.note_success("Ensembl")
            return sequence.upper() if isinstance(sequence, str) and sequence else None
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                f"Ensembl CDS-sequence fetch failed for {transcript_id}: {exc}. PVS1 will fall back to "
                "the local protein-translation window for substitution consequences."
            )
            return None

    @staticmethod
    def _choose_transcript(transcripts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Pick the transcript ACMG/AMP interpretation should be performed
        against: the canonical protein-coding one (GENCODE canonical is
        MANE Select wherever MANE covers the gene). Falls back to the
        protein-coding transcript with the longest translation, so a gene
        without a canonical flag still yields a usable structure rather
        than none.
        """
        coding = [
            t for t in transcripts if t.get("biotype") == "protein_coding" and t.get("Translation") and t.get("Exon")
        ]
        if not coding:
            return None
        canonical = [t for t in coding if t.get("is_canonical")]
        if canonical:
            return canonical[0]
        return max(coding, key=lambda t: (t.get("Translation") or {}).get("length") or 0)
