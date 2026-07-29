"""
Orphanet gene-disorder query providers.

Unlike every other evidence source in this codebase (ClinGen/HPO/
InterPro/AlphaFold), there is deliberately only ONE provider tier here,
not a local-dataset/live-API pair: Orphanet's REST API
(`api.orphadata.com`) is part of "Orphadata Products", gated behind a
paid Data Transfer Agreement or Service Contract (verified live against
https://www.orphadata.com's legal notice and https://disgenet... no --
https://www.orphadata.com/plans-equivalent pricing pages during
development; `api.orphadata.com` returns a structured 404 for
guessed paths, i.e. it is a real, live, but contract-gated service, not
merely undocumented).

The only self-serve, no-contract, commercially-usable access path is
the free CC BY 4.0 "Orphadata Science" bulk download
(`en_product6.xml`, "genes associated with rare diseases") -- so that
bulk file, self-provisioned by `pipeline/orphanet/bootstrap.py`
exactly like `pipeline/hpo/provider.py::LocalDatasetHPOProvider` does
for HPO's own bulk file, *is* the only provider. There is no live-API
fallback to add: a gene missing from this dataset is genuinely not
covered by GEPER's Orphanet integration, not a case where a live call
would find more (unlike ClinGen/HPO's local-dataset-then-API shape).
"""

from __future__ import annotations

import os
from threading import Lock
from typing import Dict, List, Optional

from config import CONFIG
from pipeline.orphanet import bootstrap as orphanet_bootstrap
from pipeline.orphanet.models import OrphanetDisorderAssociation, OrphanetGeneEvidence
from pipeline.orphanet.utils import normalize_gene_symbol, parse_gene_disorder_xml
from utils.logger import get_logger

logger = get_logger(__name__)


class OrphanetProviderBase:
    """Common interface an Orphanet data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override point
        raise NotImplementedError

    def query(self, gene_symbol: str) -> Optional[OrphanetGeneEvidence]:
        """Return an OrphanetGeneEvidence, or None if this provider cannot answer at all."""
        raise NotImplementedError


class LocalDatasetOrphanetProvider(OrphanetProviderBase):
    """
    Reads Orphanet's official `en_product6.xml` gene-disorder
    association download from disk, indexed by gene symbol on first
    use (streaming-parsed -- see `pipeline/orphanet/utils.py::parse_gene_disorder_xml`
    for why: the real file is ~22MB).
    """

    name = "local_dataset"

    def __init__(self, local_file_path: Optional[str] = None, auto_fetch: bool = True):
        # Same reasoning as `LocalDatasetHPOProvider.__init__`: an
        # explicit path always wins; only when none is given does this
        # reach for a self-fetched copy, and `auto_fetch=False` exists
        # for tests that want a fully offline, deterministic provider.
        self.local_file_path = local_file_path or CONFIG.orphanet.LOCAL_FILE or None
        self._auto_fetch = auto_fetch and local_file_path is None
        self._lock = Lock()
        self._loaded = False
        self._by_gene: Dict[str, List[OrphanetDisorderAssociation]] = {}
        self._data_version: Optional[str] = None

    def is_available(self) -> bool:
        if self.local_file_path:
            return True
        return self._auto_fetch and CONFIG.orphanet.AUTO_FETCH_ENABLED and not CONFIG.orphanet.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock
                return
            path = self.local_file_path
            if self._auto_fetch and CONFIG.orphanet.AUTO_FETCH_ENABLED and not CONFIG.orphanet.OFFLINE_MODE and not path:
                path = orphanet_bootstrap.ensure_gene_disorder_file()
            if path:
                self._load(path)
            self._loaded = True

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(f"Orphanet gene-disorder local file not found at '{path}'; local dataset lookups will be empty.")
            return
        try:
            by_gene, data_version = parse_gene_disorder_xml(path)
        except Exception as exc:  # noqa: BLE001 - a malformed/truncated download must not crash the pipeline
            logger.warning(f"Could not parse Orphanet gene-disorder file '{path}': {exc}")
            return
        self._by_gene = by_gene
        self._data_version = data_version
        total_rows = sum(len(v) for v in by_gene.values())
        logger.info(f"Loaded {total_rows} Orphanet gene-disorder association(s) for {len(by_gene)} gene(s) from '{path}' (release {data_version}).")

    def query(self, gene_symbol: str) -> Optional[OrphanetGeneEvidence]:
        if not self.is_available():
            return None
        self._ensure_loaded()

        gene_symbol = normalize_gene_symbol(gene_symbol)
        associations = self._by_gene.get(gene_symbol, [])
        if not associations:
            return OrphanetGeneEvidence.not_found(gene_symbol, self.name, data_version=self._data_version)

        return OrphanetGeneEvidence(
            gene_symbol=gene_symbol,
            source=self.name,
            found=True,
            disorder_associations=associations,
            data_version=self._data_version,
        )


class CompositeOrphanetProvider:
    """
    Single-tier composite (local dataset only -- see this module's
    docstring for why there is no live-API tier to fall back to).
    Kept as its own class, matching every other evidence source's
    `Composite*Provider` shape, so `OrphanetLookup` and its tests don't
    need to special-case "only one provider exists" -- and so a future
    licensed Orphadata Products API key could be added here as a
    second tier without changing `OrphanetLookup` at all.
    """

    def __init__(self, local_provider: Optional[LocalDatasetOrphanetProvider] = None):
        self.local_provider = local_provider or LocalDatasetOrphanetProvider()

    def query(self, gene_symbol: str) -> OrphanetGeneEvidence:
        if CONFIG.orphanet.OFFLINE_MODE:
            result = self._try(gene_symbol)
            if result is not None:
                return result
            return OrphanetGeneEvidence.from_error(gene_symbol, "offline mode: no local Orphanet dataset configured")

        result = self._try(gene_symbol)
        if result is not None:
            return result
        return OrphanetGeneEvidence.not_found(gene_symbol, "none")

    def _try(self, gene_symbol: str) -> Optional[OrphanetGeneEvidence]:
        try:
            return self.local_provider.query(gene_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Orphanet provider '{self.local_provider.name}' raised unexpectedly: {exc}")
            return OrphanetGeneEvidence.from_error(gene_symbol, f"{self.local_provider.name} raised: {exc}")

    def batch_query(self, gene_symbols: List[str]) -> List[OrphanetGeneEvidence]:
        # No thread pool needed (unlike HPO's/InterPro's batch_query):
        # every lookup here is an in-memory dict read against the
        # already-loaded index, not an I/O-bound network/file call.
        return [self.query(g) for g in gene_symbols]
