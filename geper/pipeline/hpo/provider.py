"""
HPO gene-phenotype query providers.

Two concrete sources, the same local-first/API-fallback shape as
`pipeline/clingen/provider.py`:

  - `LocalDatasetHPOProvider`: reads HPO's own official Gene-to-Phenotype
    annotation download (`genes_to_phenotype.txt`, from
    http://purl.obolibrary.org/obo/hp/hpoa/) that a deployer provisions
    locally, or that `pipeline/hpo/bootstrap.py` self-fetches when none
    is configured -- the "download once, query locally, no live network
    dependency" shape this codebase prefers. This is the *primary*
    source: HPO annotation changes on the order of its periodic
    releases, not per-request, and it carries per-row detail (a
    specific disease_id + observed-frequency fraction per phenotype)
    the live API's per-gene endpoint does not.
  - `LiveAPIHPOProvider`: the Monarch/JAX-hosted HPO API
    (https://ontology.jax.org/api), used as a fallback for a gene not
    present in the local dataset. UNLIKE `pipeline/clingen/provider.py::
    LiveAPIClinGenProvider`, this endpoint was exercised against real,
    live requests during development and confirmed working:
      - `GET /api/network/search/GENE?q={symbol}` -> `{"results": [{"id": "NCBIGene:2200", "name": "FBN1"}, ...]}`
        (verified live for FBN1 -> NCBIGene:2200, CFTR -> NCBIGene:1080)
      - `GET /api/network/annotation/{ncbi_gene_id}` -> `{"diseases": [...], "phenotypes": [...]}`
        (verified live: NCBIGene:2200 -> 17 diseases incl. OMIM:154700
        "Marfan syndrome", 312 phenotypes incl. HP:0002705 "High, narrow
        palate")
    No "unverified, re-check before production" caveat applies here.

`CompositeHPOProvider` tries the local dataset first, then the live
API, honoring offline mode, and never raises out of
`query()`/`batch_query()` -- callers always get an `HPOGeneEvidence`
back (possibly with `.error` set), matching this codebase's
graceful-degradation policy for every external evidence source.
"""

from __future__ import annotations

import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.hpo import bootstrap as hpo_bootstrap
from pipeline.hpo.models import HPODiseaseAssociation, HPOGeneEvidence, HPOPhenotypeAssociation
from pipeline.hpo.utils import gene_cache_key, normalize_gene_symbol, parse_genes_to_phenotype_row
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class HPOProviderBase:
    """Common interface every HPO data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, gene_symbol: str) -> Optional[HPOGeneEvidence]:
        """Return an HPOGeneEvidence, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local curated-dataset provider
# ---------------------------------------------------------------------------


class LocalDatasetHPOProvider(HPOProviderBase):
    """
    Reads HPO's official Gene-to-Phenotype annotation download from
    disk, indexed by gene symbol on first use. A plain TSV with a
    single header row (no preamble to skip, unlike ClinGen's two
    download shapes) -- verified live against the real file during
    development.
    """

    name = "local_dataset"

    def __init__(self, local_file_path: Optional[str] = None, auto_fetch: bool = True):
        # Same reasoning as `LocalDatasetClinGenProvider.__init__`: an
        # explicit path always wins; only when none is given does this
        # reach for a self-fetched copy, and `auto_fetch=False` exists
        # for tests that want a fully offline, deterministic provider.
        self.local_file_path = local_file_path or CONFIG.hpo.LOCAL_FILE or None
        self._auto_fetch = auto_fetch and local_file_path is None
        self._lock = Lock()
        self._loaded = False
        self._by_gene: Dict[str, List[HPOPhenotypeAssociation]] = {}

    def is_available(self) -> bool:
        if self.local_file_path:
            return True
        return self._auto_fetch and CONFIG.hpo.AUTO_FETCH_ENABLED and not CONFIG.hpo.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock
                return
            path = self.local_file_path
            if self._auto_fetch and CONFIG.hpo.AUTO_FETCH_ENABLED and not CONFIG.hpo.OFFLINE_MODE and not path:
                path = hpo_bootstrap.ensure_genes_to_phenotype_file()
            if path:
                self._load(path)
            self._loaded = True

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(f"HPO gene-to-phenotype local file not found at '{path}'; local dataset lookups will be empty.")
            return
        count = 0
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                for row in reader:
                    parsed = parse_genes_to_phenotype_row(row)
                    if parsed is None:
                        continue
                    self._by_gene.setdefault(parsed.gene_symbol, []).append(parsed)
                    count += 1
        except OSError as exc:
            logger.warning(f"Could not read HPO gene-to-phenotype file '{path}': {exc}")
            return
        logger.info(f"Loaded {count} HPO gene-phenotype association(s) from '{path}'.")

    def query(self, gene_symbol: str) -> Optional[HPOGeneEvidence]:
        if not self.is_available():
            return None
        self._ensure_loaded()

        gene_symbol = normalize_gene_symbol(gene_symbol)
        associations = self._by_gene.get(gene_symbol, [])
        if not associations:
            return HPOGeneEvidence.not_found(gene_symbol, self.name)

        ncbi_gene_id = next((a.ncbi_gene_id for a in associations if a.ncbi_gene_id), None)
        return HPOGeneEvidence(
            gene_symbol=gene_symbol,
            source=self.name,
            found=True,
            phenotype_associations=associations,
            ncbi_gene_id=ncbi_gene_id,
        )


# ---------------------------------------------------------------------------
# Live API
# ---------------------------------------------------------------------------


class LiveAPIHPOProvider(HPOProviderBase):
    """
    Queries the Monarch/JAX-hosted HPO live API. See this module's
    docstring for the two real, verified-live endpoints this uses.
    """

    name = "api"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = (endpoint or CONFIG.hpo.API_ENDPOINT).rstrip("/")

    def is_available(self) -> bool:
        return bool(CONFIG.hpo.API_ENABLED) and not CONFIG.hpo.OFFLINE_MODE

    def query(self, gene_symbol: str) -> Optional[HPOGeneEvidence]:
        if not self.is_available():
            return None
        gene_symbol = normalize_gene_symbol(gene_symbol)
        try:
            ncbi_gene_id = self._search_gene_id(gene_symbol)
        except ExternalAPIError as exc:
            logger.warning(f"HPO API gene search failed for gene '{gene_symbol}': {exc}")
            return HPOGeneEvidence.from_error(gene_symbol, str(exc))

        if not ncbi_gene_id:
            return HPOGeneEvidence.not_found(gene_symbol, self.name)

        try:
            payload = self._get_annotation(ncbi_gene_id)
        except ExternalAPIError as exc:
            logger.warning(f"HPO API annotation lookup failed for gene '{gene_symbol}' ({ncbi_gene_id}): {exc}")
            return HPOGeneEvidence.from_error(gene_symbol, str(exc))

        return _evidence_from_annotation_payload(gene_symbol, ncbi_gene_id, payload, self.name)

    def _search_gene_id(self, gene_symbol: str) -> Optional[str]:
        """
        `GET /api/network/search/GENE?q={symbol}` -> `{"results": [{"id": "NCBIGene:2200", "name": "FBN1"}, ...]}`.
        Prefers an exact (case-insensitive) name match over the first
        result -- the search is prefix/substring-based and can return
        related entries (e.g. "FBN1" also returns "FBN1-DT", the
        divergent-transcript gene) that must not be silently picked
        over the exact gene requested.
        """
        payload = self._get_json(f"{self.endpoint}/network/search/GENE", {"q": gene_symbol})
        results = (payload or {}).get("results") or []
        for entry in results:
            if (entry.get("name") or "").strip().upper() == gene_symbol:
                return entry.get("id")
        return None

    def _get_annotation(self, ncbi_gene_id: str) -> Dict[str, Any]:
        """`GET /api/network/annotation/{ncbi_gene_id}` -> `{"diseases": [...], "phenotypes": [...]}`."""
        return self._get_json(f"{self.endpoint}/network/annotation/{ncbi_gene_id}", {}) or {}

    def _get_json(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.hpo.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.hpo.QUERY_TIMEOUT_SECS)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"HPO API request attempt {attempt} failed for '{url}': {exc}")
                if attempt < CONFIG.hpo.MAX_RETRIES:
                    time.sleep(CONFIG.hpo.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(f"HPO API request to '{url}' failed after {CONFIG.hpo.MAX_RETRIES} attempts: {last_error}")


def _evidence_from_annotation_payload(
    gene_symbol: str, ncbi_gene_id: str, payload: Dict[str, Any], source: str
) -> HPOGeneEvidence:
    phenotypes = payload.get("phenotypes") or []
    diseases = payload.get("diseases") or []

    if not phenotypes and not diseases:
        return HPOGeneEvidence.not_found(gene_symbol, source)

    # The live annotation endpoint reports phenotypes gene-wide, not
    # per-disease -- unlike the local dataset's per-row disease_id/
    # frequency, so those two fields are honestly left None here
    # rather than guessed (see `HPOPhenotypeAssociation`'s docstring).
    phenotype_associations = [
        HPOPhenotypeAssociation(
            gene_symbol=gene_symbol,
            hpo_id=p.get("id"),
            hpo_name=p.get("name"),
            ncbi_gene_id=ncbi_gene_id,
        )
        for p in phenotypes
        if p.get("id") and p.get("name")
    ]
    disease_associations = [
        HPODiseaseAssociation(disease_id=d.get("id"), disease_name=d.get("name"), mondo_id=d.get("mondoId"))
        for d in diseases
        if d.get("id") and d.get("name")
    ]

    return HPOGeneEvidence(
        gene_symbol=gene_symbol,
        source=source,
        found=True,
        phenotype_associations=phenotype_associations,
        disease_associations=disease_associations,
        ncbi_gene_id=ncbi_gene_id,
    )


# ---------------------------------------------------------------------------
# Composite: local-first, API-fallback, sync + async batch
# ---------------------------------------------------------------------------


class CompositeHPOProvider:
    """
    Tries the local curated-dataset provider first (when configured),
    then the live API fallback (unless offline mode is on) -- the same
    local-first/API-fallback shape as
    `pipeline/clingen/provider.py::CompositeClinGenProvider`. Never
    raises: any provider failure is logged and reported inside the
    returned `HPOGeneEvidence.error`.
    """

    def __init__(
        self,
        local_provider: Optional[LocalDatasetHPOProvider] = None,
        api_provider: Optional[LiveAPIHPOProvider] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalDatasetHPOProvider()
        self.api_provider = api_provider or LiveAPIHPOProvider()
        self.max_concurrent = max_concurrent or CONFIG.hpo.MAX_CONCURRENT

    def query(self, gene_symbol: str) -> HPOGeneEvidence:
        gene_symbol = normalize_gene_symbol(gene_symbol)
        if not gene_symbol:
            return HPOGeneEvidence.from_error("", "no gene symbol provided")

        if CONFIG.hpo.OFFLINE_MODE:
            result = self._try(self.local_provider, gene_symbol)
            if result is not None:
                return result
            return HPOGeneEvidence.from_error(
                gene_symbol, "offline mode: no local HPO dataset configured (or gene not present in it)"
            )

        result = self._try(self.local_provider, gene_symbol)
        if result is not None and result.found:
            return result

        api_result = self._try(self.api_provider, gene_symbol)
        if api_result is not None and (api_result.found or api_result.error):
            return api_result

        # Neither source found the gene (as opposed to erroring) --
        # prefer returning the local provider's clean "not found" over
        # a None if it answered at all, so callers can distinguish "we
        # checked and there's no HPO annotation for this gene" from "no
        # source could be reached".
        if result is not None:
            return result
        if api_result is not None:
            return api_result
        return HPOGeneEvidence.not_found(gene_symbol, "none")

    @staticmethod
    def _try(provider: HPOProviderBase, gene_symbol: str) -> Optional[HPOGeneEvidence]:
        try:
            return provider.query(gene_symbol)
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            logger.error(f"HPO provider '{provider.name}' raised unexpectedly: {exc}")
            return HPOGeneEvidence.from_error(gene_symbol, f"{provider.name} raised: {exc}")

    # -- batch: thread-pooled -------------------------------------------

    def batch_query(self, gene_symbols: List[str]) -> List[HPOGeneEvidence]:
        """Concurrent (thread-pooled, since each query is I/O-bound: a file lookup or HTTP round trip)."""
        if not gene_symbols:
            return []
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            return list(executor.map(self.query, gene_symbols))
