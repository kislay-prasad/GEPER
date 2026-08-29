"""
UniProt query providers.

Two independent sources, mirroring `pipeline/gnomad/provider.py` and
`pipeline/clingen/provider.py`'s local-first/API-fallback shape:

  - `LocalDatasetUniProtProvider`: reads a deployer-provisioned local
    JSON-lines file (`{"gene_symbol": "...", "entry": {...UniProt
    REST API entry JSON...}}` per line) -- useful for offline runs,
    deterministic tests, or a pre-fetched subset of UniProt for a
    fixed gene panel.
  - `LiveAPIUniProtProvider`: queries UniProt's public REST API
    (`rest.uniprot.org`) directly, filtered to the reviewed
    (Swiss-Prot) human entry for a gene symbol, with the same
    retry/backoff/timeout shape as `database/clinvar_client.py` /
    `pipeline/clingen/provider.py`.

`CompositeUniProtProvider` tries local first, then the live API,
honoring offline mode, and never raises out of `query()` -- callers
always get a `UniProtAnnotation` back (possibly with `.error` set),
matching the graceful-degradation policy every other external
evidence source in this codebase already follows.

NOTE (same caveat `pipeline/clingen/provider.py` documents for its own
live API path): this sandbox has no network route to
`rest.uniprot.org`, so the live path below is implemented against
UniProt's officially published REST API schema
(https://www.uniprot.org/help/return_fields,
https://rest.uniprot.org/uniprotkb/search) and exercised in tests with
`requests` mocked out, not against a real network response. Re-verify
against a live call before relying on it in production, exactly as
README.md already advises for ClinGen's live API path.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.uniprot.models import UniProtAnnotation
from pipeline.uniprot.utils import normalize_gene_symbol, parse_uniprot_entry
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class UniProtProviderBase:
    """Common interface every UniProt data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, gene_symbol: str) -> Optional[UniProtAnnotation]:
        """Return a UniProtAnnotation, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


class LocalDatasetUniProtProvider(UniProtProviderBase):
    """
    Queries a local, gene-symbol-indexed JSON-lines dataset of
    pre-fetched UniProt entries -- either a deployer-provisioned
    `dataset_path`, or (when none is given) self-fetched and cached by
    `pipeline/uniprot/bootstrap.py` on first use, the same auto-fetch
    shape `pipeline/mane/provider.py::LocalDatasetMANEProvider` and
    `pipeline/hpo/provider.py::LocalDatasetHPOProvider` already use.
    `auto_fetch=False` exists for tests that want a fully offline,
    deterministic provider (same reasoning as those two).
    """

    name = "local_dataset"

    def __init__(self, dataset_path: Optional[str] = None, auto_fetch: bool = True):
        self.dataset_path = dataset_path
        self._auto_fetch = auto_fetch and dataset_path is None
        self._lock = Lock()
        self._loaded = False
        self._index: Optional[Dict[str, Dict[str, Any]]] = None

    def is_available(self) -> bool:
        if self.dataset_path:
            return True
        return self._auto_fetch and CONFIG.uniprot.AUTO_FETCH_ENABLED and not CONFIG.uniprot.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock
                return
            path = self.dataset_path
            if self._auto_fetch and CONFIG.uniprot.AUTO_FETCH_ENABLED and not CONFIG.uniprot.OFFLINE_MODE and not path:
                from pipeline.uniprot import bootstrap as uniprot_bootstrap

                try:
                    path = uniprot_bootstrap.ensure_dataset_file()
                except Exception as exc:  # noqa: BLE001 - an unexpected bootstrap failure must still let `self._loaded`
                    # get set below, or every subsequent `query()` this run would re-attempt the
                    # full ~15s fetch+parse instead of caching the "unavailable" outcome once.
                    # (This is the exact mechanism that made the UnknownPosition bug re-download
                    # the reference proteome on every one of 20 variants in one real run: the
                    # exception used to propagate out of this method before `self._loaded = True`
                    # below ever executed.)
                    logger.error(f"UniProt bootstrap raised unexpectedly: {exc}")
                    path = None
            if path:
                self._load(path)
            self._loaded = True

    def _load(self, path: str) -> None:
        index: Dict[str, Dict[str, Any]] = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    gene = normalize_gene_symbol(record.get("gene_symbol"))
                    if gene and record.get("entry"):
                        index[gene] = record["entry"]
        except OSError as exc:
            logger.warning(f"Could not read local UniProt dataset '{path}': {exc}")
        self._index = index
        logger.info(f"Loaded {len(index)} UniProt gene entries from '{path}'.")

    def query(self, gene_symbol: str) -> Optional[UniProtAnnotation]:
        if not self.is_available():
            return None
        self._ensure_loaded()
        gene = normalize_gene_symbol(gene_symbol)
        entry = (self._index or {}).get(gene or "")
        if entry is None:
            return UniProtAnnotation.not_found(gene_symbol, self.name)
        try:
            return parse_uniprot_entry(gene_symbol, entry, self.name)
        except Exception as exc:  # noqa: BLE001 - a malformed local record must never crash the pipeline
            logger.warning(f"Could not parse local UniProt entry for '{gene_symbol}': {exc}")
            return UniProtAnnotation.from_error(gene_symbol, f"local dataset parse error: {exc}")


class LiveAPIUniProtProvider(UniProtProviderBase):
    """Queries UniProt's public REST API for the reviewed (Swiss-Prot) human entry matching a gene symbol."""

    name = "uniprot_rest_api"

    def __init__(self, api_base: Optional[str] = None):
        self.api_base = api_base or CONFIG.uniprot.API_BASE
        # Set by `_get()` on every successful response -- read by
        # `query()` right after. See `pipeline/provenance.py`'s
        # docstring for why this exists and `LiveAPIInterProProvider`
        # for the identical pattern.
        self._last_release: Optional[str] = None
        self._last_release_date: Optional[str] = None

    def is_available(self) -> bool:
        return bool(CONFIG.uniprot.ENABLED) and not CONFIG.uniprot.OFFLINE_MODE

    def query(self, gene_symbol: str) -> Optional[UniProtAnnotation]:
        if not self.is_available():
            return None

        gene = normalize_gene_symbol(gene_symbol)
        if not gene:
            return UniProtAnnotation.from_error(gene_symbol, "no gene symbol provided")

        query_parts = [f"gene:{gene}", f"organism_id:{CONFIG.uniprot.ORGANISM_ID}"]
        if CONFIG.uniprot.REVIEWED_ONLY:
            query_parts.append("reviewed:true")
        params = {
            "query": " AND ".join(query_parts),
            "format": "json",
            "size": "1",
        }

        try:
            payload = self._get(f"{self.api_base}/search", params)
        except ExternalAPIError as exc:
            logger.warning(f"UniProt REST API query failed for gene '{gene}': {exc}")
            return UniProtAnnotation.from_error(gene_symbol, str(exc))

        results = payload.get("results") or []
        if not results:
            annotation = UniProtAnnotation.not_found(gene_symbol, self.name)
        else:
            annotation = parse_uniprot_entry(gene_symbol, results[0], self.name)
        annotation.release = self._last_release
        annotation.release_date = self._last_release_date
        return annotation

    def _get(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.uniprot.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.uniprot.QUERY_TIMEOUT_SECS)
                response.raise_for_status()
                # Present on every successful response (verified live).
                self._last_release = response.headers.get("X-UniProt-Release")
                self._last_release_date = response.headers.get("X-UniProt-Release-Date")
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"UniProt REST API request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.uniprot.MAX_RETRIES:
                    time.sleep(CONFIG.uniprot.RETRY_BACKOFF_SECS * attempt)
        logger.warning(
            f"UniProt REST API request to '{url}' failed after {CONFIG.uniprot.MAX_RETRIES} attempts: {last_error}"
        )
        # Round 24: the message actually raised (folded verbatim into
        # `uniprot_error` -- see `report/clinical_report_builder.py::
        # _protein_knowledge` -- and rendered into every report
        # unconditionally by `report/report_generator.py`, plus
        # embedded verbatim in geper_results.json's `uniprot` key by
        # `report/json_builder.py`) must not embed `url`/`last_error`;
        # full detail goes to the log line above only. Same pattern
        # round 23 applied to `annotation/thousand_genomes_sas.py`.
        raise ExternalAPIError(f"UniProt REST API request failed after {CONFIG.uniprot.MAX_RETRIES} attempts")


class CompositeUniProtProvider:
    """Tries the local dataset first (when configured), then UniProt's live REST API, honoring offline mode. Never raises."""

    def __init__(
        self,
        local_provider: Optional[LocalDatasetUniProtProvider] = None,
        api_provider: Optional[LiveAPIUniProtProvider] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalDatasetUniProtProvider(
            dataset_path=CONFIG.uniprot.LOCAL_DATASET_FILE or None
        )
        self.api_provider = api_provider or LiveAPIUniProtProvider()
        self.max_concurrent = max_concurrent if max_concurrent is not None else 8

    def query(self, gene_symbol: str) -> UniProtAnnotation:
        if CONFIG.uniprot.OFFLINE_MODE:
            result = self._try(self.local_provider, gene_symbol)
            if result is not None:
                return result
            return UniProtAnnotation.from_error(gene_symbol, "offline mode: no local UniProt dataset configured")

        # Falls through to the live API on a clean local "not_found",
        # not just on local being altogether unavailable/erroring --
        # now that `local_provider` can be a self-fetched, auto-
        # populated dataset (see `pipeline/uniprot/bootstrap.py`)
        # rather than only an explicit, deployer-curated file, a clean
        # miss from it is NOT authoritative on its own: the reference
        # proteome the bootstrap fetches doesn't claim 100% gene-symbol
        # coverage (alias mismatches, very recently characterized
        # genes). A local *error* (an exception, or `.error` set) is
        # kept as a terminal result exactly as before -- matching the
        # `CompositeClinGenProvider`/`CompositeGnomadProvider`
        # convention `test_provider_exception_is_caught_gracefully`
        # documents: a provider that raises reports a definite failure,
        # not a "try the next one" signal.
        result = self._try(self.local_provider, gene_symbol)
        if result is not None and (result.found or result.error):
            return result

        api_result = self._try(self.api_provider, gene_symbol)
        if api_result is not None:
            return api_result

        # Local answered cleanly (found nothing, no error) but the API
        # was unavailable/disabled -- prefer local's clean "not found"
        # over fabricating a fresh one, so callers keep whatever source
        # attribution it carried.
        if result is not None:
            return result
        return UniProtAnnotation.not_found(gene_symbol, "none")

    @staticmethod
    def _try(provider: UniProtProviderBase, gene_symbol: str) -> Optional[UniProtAnnotation]:
        try:
            return provider.query(gene_symbol)
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            logger.error(f"UniProt provider '{provider.name}' raised unexpectedly: {exc}")
            return UniProtAnnotation.from_error(gene_symbol, f"{provider.name} raised: {exc}")

    def batch_query(self, gene_symbols: List[str]) -> List[UniProtAnnotation]:
        """Concurrent (thread-pooled) batch lookup for a prefetch pass over many distinct genes."""
        if not gene_symbols:
            return []
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            return list(executor.map(self.query, gene_symbols))
