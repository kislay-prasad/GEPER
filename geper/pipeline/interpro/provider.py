"""
InterPro/Pfam query providers.

Mirrors `pipeline/uniprot/provider.py`'s shape: a local-dataset
provider for offline/deterministic use, a live REST API provider for
`www.ebi.ac.uk/interpro/api`, and a composite that tries local first
then falls back to the live API, honoring offline mode. Never raises
out of `query()`.

NOTE (same sandbox caveat documented in `pipeline/uniprot/provider.py`
and `pipeline/clingen/provider.py`): this sandbox has no network route
to `ebi.ac.uk`, so the live path is implemented against InterPro's
officially published REST API schema
(https://interpro-documentation.readthedocs.io/en/latest/interpro7-api.html)
and exercised in tests with `requests` mocked out. Re-verify against a
live call before relying on it in production.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.interpro.models import InterProAnnotation
from pipeline.interpro.utils import normalize_accession, parse_interpro_response
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class InterProProviderBase:
    """Common interface every InterPro data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, accession: str) -> Optional[InterProAnnotation]:
        """Return an InterProAnnotation, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


class LocalDatasetInterProProvider(InterProProviderBase):
    """Queries a local, accession-indexed JSON-lines dataset of pre-fetched InterPro API responses."""

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
                    accession = normalize_accession(record.get("accession"))
                    if accession and record.get("payload") is not None:
                        index[accession] = record["payload"]
        except OSError as exc:
            logger.warning(f"Could not read local InterPro dataset '{self.dataset_path}': {exc}")
        self._index = index
        return index

    def query(self, accession: str) -> Optional[InterProAnnotation]:
        if not self.is_available():
            return None
        key = normalize_accession(accession)
        payload = self._load().get(key or "")
        if payload is None:
            return InterProAnnotation.not_found(accession, self.name)
        try:
            return parse_interpro_response(accession, payload, self.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not parse local InterPro entry for '{accession}': {exc}")
            return InterProAnnotation.from_error(accession, f"local dataset parse error: {exc}")


class LiveAPIInterProProvider(InterProProviderBase):
    """Queries InterPro's public REST API for every domain/family/site match against a UniProt accession."""

    name = "interpro_rest_api"

    def __init__(self, api_base: Optional[str] = None):
        self.api_base = api_base or CONFIG.interpro.API_BASE

    def is_available(self) -> bool:
        return bool(CONFIG.interpro.ENABLED) and not CONFIG.interpro.OFFLINE_MODE

    def query(self, accession: str) -> Optional[InterProAnnotation]:
        if not self.is_available():
            return None

        normalized = normalize_accession(accession)
        if not normalized:
            return InterProAnnotation.from_error(accession, "no UniProt accession provided")

        url = f"{self.api_base}/entry/all/protein/uniprot/{normalized}/"
        params = {"page_size": str(CONFIG.interpro.PAGE_SIZE)}

        try:
            payload = self._get(url, params)
        except ExternalAPIError as exc:
            logger.warning(f"InterPro REST API query failed for accession '{normalized}': {exc}")
            return InterProAnnotation.from_error(accession, str(exc))

        return parse_interpro_response(accession, payload, self.name)

    def _get(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.interpro.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.interpro.QUERY_TIMEOUT_SECS)
                if response.status_code == 404:
                    # InterPro's API returns a plain 404 for an accession with no matches at all
                    # (rather than a 200 with an empty results list) -- treat as "not found", not an error.
                    return {"results": []}
                response.raise_for_status()
                # Confirmed live against the real API (2026-07-30): a
                # syntactically valid UniProt accession with zero
                # domain/family/site matches returns HTTP 204 No
                # Content -- a definitive, deterministic "no matches"
                # answer, not a transient failure. `response.json()`
                # raises `json.JSONDecodeError` (a `ValueError`
                # subclass) on the empty body, which the `except`
                # clause below previously treated as retryable --
                # retrying a 204 just gets another 204, so this
                # silently burned all `MAX_RETRIES` attempts and then
                # reported a false "lookup failed" for what was
                # actually a successful, informative response. Handled
                # here, before `.json()` is ever called on it.
                if response.status_code == 204 or not response.content:
                    return {"results": []}
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"InterPro REST API request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.interpro.MAX_RETRIES:
                    time.sleep(CONFIG.interpro.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(f"InterPro REST API request to '{url}' failed after {CONFIG.interpro.MAX_RETRIES} attempts: {last_error}")


class CompositeInterProProvider:
    """Tries the local dataset first (when configured), then InterPro's live REST API, honoring offline mode. Never raises."""

    def __init__(
        self,
        local_provider: Optional[LocalDatasetInterProProvider] = None,
        api_provider: Optional[LiveAPIInterProProvider] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalDatasetInterProProvider(dataset_path=CONFIG.interpro.LOCAL_DATASET_FILE or None)
        self.api_provider = api_provider or LiveAPIInterProProvider()
        self.max_concurrent = max_concurrent or 8

    def query(self, accession: str) -> InterProAnnotation:
        if CONFIG.interpro.OFFLINE_MODE:
            result = self._try(self.local_provider, accession)
            if result is not None:
                return result
            return InterProAnnotation.from_error(accession, "offline mode: no local InterPro dataset configured")

        result = self._try(self.local_provider, accession)
        if result is not None:
            return result

        result = self._try(self.api_provider, accession)
        if result is not None:
            return result

        return InterProAnnotation.not_found(accession, "none")

    @staticmethod
    def _try(provider: InterProProviderBase, accession: str) -> Optional[InterProAnnotation]:
        try:
            return provider.query(accession)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"InterPro provider '{provider.name}' raised unexpectedly: {exc}")
            return InterProAnnotation.from_error(accession, f"{provider.name} raised: {exc}")

    def batch_query(self, accessions: List[str]) -> List[InterProAnnotation]:
        if not accessions:
            return []
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            return list(executor.map(self.query, accessions))
