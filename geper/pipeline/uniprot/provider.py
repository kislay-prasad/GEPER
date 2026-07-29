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
import os
import time
from concurrent.futures import ThreadPoolExecutor
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
    """Queries a local, gene-symbol-indexed JSON-lines dataset of pre-fetched UniProt entries."""

    name = "local_dataset"

    def __init__(self, dataset_path: Optional[str] = None):
        self.dataset_path = dataset_path
        self._index: Optional[Dict[str, Dict[str, Any]]] = None

    def is_available(self) -> bool:
        return bool(self.dataset_path) and os.path.exists(self.dataset_path)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._index is not None:
            return self._index
        index: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self.dataset_path, "r", encoding="utf-8") as fh:
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
            logger.warning(f"Could not read local UniProt dataset '{self.dataset_path}': {exc}")
        self._index = index
        return index

    def query(self, gene_symbol: str) -> Optional[UniProtAnnotation]:
        if not self.is_available():
            return None
        gene = normalize_gene_symbol(gene_symbol)
        entry = self._load().get(gene or "")
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
            return UniProtAnnotation.not_found(gene_symbol, self.name)

        return parse_uniprot_entry(gene_symbol, results[0], self.name)

    def _get(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.uniprot.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.uniprot.QUERY_TIMEOUT_SECS)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"UniProt REST API request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.uniprot.MAX_RETRIES:
                    time.sleep(CONFIG.uniprot.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(f"UniProt REST API request to '{url}' failed after {CONFIG.uniprot.MAX_RETRIES} attempts: {last_error}")


class CompositeUniProtProvider:
    """Tries the local dataset first (when configured), then UniProt's live REST API, honoring offline mode. Never raises."""

    def __init__(
        self,
        local_provider: Optional[LocalDatasetUniProtProvider] = None,
        api_provider: Optional[LiveAPIUniProtProvider] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalDatasetUniProtProvider(dataset_path=CONFIG.uniprot.LOCAL_DATASET_FILE or None)
        self.api_provider = api_provider or LiveAPIUniProtProvider()
        self.max_concurrent = max_concurrent or 8

    def query(self, gene_symbol: str) -> UniProtAnnotation:
        if CONFIG.uniprot.OFFLINE_MODE:
            result = self._try(self.local_provider, gene_symbol)
            if result is not None:
                return result
            return UniProtAnnotation.from_error(gene_symbol, "offline mode: no local UniProt dataset configured")

        result = self._try(self.local_provider, gene_symbol)
        if result is not None:
            return result

        result = self._try(self.api_provider, gene_symbol)
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
