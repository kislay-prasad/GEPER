"""
OrphanetLookup: the facade `pipeline/orchestrator.py` talks to, in the
same shape as `pipeline.hpo.lookup.HPOLookup` -- a cached, never-raises
gene-evidence lookup safe to call once per variant inside the main
per-variant loop.

Like HPO, this facade does its own no-op variant->gene step: Orphanet
annotation is purely gene-level, and the orchestrator already resolves
a variant's gene symbol via the ClinGen stage
(`pipeline.clingen.utils.resolve_gene_symbol`) that runs before this
one -- so `query_variant` takes that gene symbol directly, the same
reuse `HPOLookup.query_variant` documents.

Layering: OrphanetLookup -> OrphanetCache -> CompositeOrphanetProvider -> LocalDatasetOrphanetProvider.
(No live-API tier -- see `pipeline/orphanet/provider.py`'s docstring.)
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.orphanet.cache import OrphanetCache
from pipeline.orphanet.models import OrphanetGeneEvidence
from pipeline.orphanet.provider import CompositeOrphanetProvider
from pipeline.orphanet.utils import gene_cache_key, normalize_gene_symbol
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {"skipped": True, "reason": "Orphanet integration disabled via GEPER_ENABLE_ORPHANET=false", "found": False}


class OrphanetLookup:
    """High-level, cached Orphanet gene-disorder evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeOrphanetProvider] = None,
        cache: Optional[OrphanetCache] = None,
    ):
        self.provider = provider or CompositeOrphanetProvider()
        self.cache = cache or (
            OrphanetCache(
                max_size=CONFIG.orphanet.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.orphanet.CACHE_TTL_SECS,
                disk_path=CONFIG.orphanet.CACHE_DISK_PATH or None,
            )
            if CONFIG.orphanet.CACHE_ENABLED
            else None
        )

    def query_gene(self, gene_symbol: str) -> Dict[str, Any]:
        """Look up Orphanet gene-disorder evidence for a gene symbol directly (used by callers that already know the gene, e.g. tests/reports)."""
        if not CONFIG.orphanet.ENABLED:
            return dict(_SKIPPED_RESULT)

        gene_symbol = normalize_gene_symbol(gene_symbol)
        if not gene_symbol:
            return {"skipped": False, "found": False, "error": "no gene symbol provided"}

        key = gene_cache_key(gene_symbol)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        evidence = self.provider.query(gene_symbol)
        result = evidence.to_dict()
        result["skipped"] = False

        if self.cache is not None and evidence.error is None:
            self.cache.put(key, result)

        return result

    def query_variant(self, gene_symbol_hint: Optional[str]) -> Dict[str, Any]:
        """
        Look up Orphanet gene-disorder evidence for the gene a variant
        overlaps. Never raises -- matches `HPOLookup.query_variant`'s
        contract. Takes the already-resolved gene symbol directly
        (see this module's docstring) rather than a `Variant`.
        """
        if not CONFIG.orphanet.ENABLED:
            return dict(_SKIPPED_RESULT)

        if not gene_symbol_hint:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "error": None,
                "reason": "no overlapping gene annotation was resolved for this position; Orphanet annotation is gene-level and requires a resolved gene symbol",
            }

        result = self.query_gene(gene_symbol_hint)
        result.setdefault("gene_symbol", normalize_gene_symbol(gene_symbol_hint))
        return result

    def query_variants_batch(self, gene_symbols: List[Optional[str]]) -> List[Dict[str, Any]]:
        """Batch lookup for an up-front prefetch pass over a whole VCF's already-resolved gene symbols, mirroring `HPOLookup.query_variants_batch`."""
        if not CONFIG.orphanet.ENABLED:
            return [dict(_SKIPPED_RESULT) for _ in gene_symbols]

        distinct_genes = sorted({normalize_gene_symbol(g) for g in gene_symbols if g})
        to_fetch = []
        cached_by_gene: Dict[str, Dict[str, Any]] = {}
        for gene in distinct_genes:
            key = gene_cache_key(gene)
            cached = self.cache.get(key) if self.cache is not None else None
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                cached_by_gene[gene] = result
            else:
                to_fetch.append(gene)

        fetched_by_gene: Dict[str, Dict[str, Any]] = {}
        if to_fetch:
            fetched = self.provider.batch_query(to_fetch)
            for gene, evidence in zip(to_fetch, fetched):
                result = evidence.to_dict()
                result["skipped"] = False
                fetched_by_gene[gene] = result
                if self.cache is not None and evidence.error is None:
                    self.cache.put(gene_cache_key(gene), result)

        results: List[Dict[str, Any]] = []
        for gene_symbol in gene_symbols:
            gene_symbol = normalize_gene_symbol(gene_symbol)
            if not gene_symbol:
                results.append({"skipped": False, "found": False, "gene_symbol": None, "reason": "no overlapping gene annotation resolved"})
                continue
            result = cached_by_gene.get(gene_symbol) or fetched_by_gene.get(gene_symbol) or {"skipped": False, "found": False}
            result = dict(result)
            result.setdefault("gene_symbol", gene_symbol)
            results.append(result)

        return results
