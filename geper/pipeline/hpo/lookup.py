"""
HPOLookup: the facade `pipeline/orchestrator.py` talks to, in the same
shape as `pipeline.clingen.lookup.ClinGenLookup` -- a cached, never-raises
gene-evidence lookup safe to call once per variant inside the main
per-variant loop.

Unlike `ClinGenLookup`, this facade does its own variant->gene
resolution: HPO annotation is purely gene-level, and the orchestrator
already resolves a variant's gene symbol via the ClinGen stage
(`pipeline.clingen.utils.resolve_gene_symbol`) that runs immediately
before this one -- so `query_variant` takes that gene symbol directly
rather than repeating the Ensembl overlap lookup, the same reuse
`_run_uniprot_stage`/`_run_transcript_stage` already do for their own
gene-symbol needs.

Layering: HPOLookup -> HPOCache -> CompositeHPOProvider ->
(LocalDatasetHPOProvider | LiveAPIHPOProvider).
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.hpo.cache import HPOCache
from pipeline.hpo.provider import CompositeHPOProvider
from pipeline.hpo.utils import gene_cache_key, normalize_gene_symbol
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {"skipped": True, "reason": "HPO integration disabled via GEPER_ENABLE_HPO=false", "found": False}


class HPOLookup:
    """High-level, cached HPO gene-phenotype evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeHPOProvider] = None,
        cache: Optional[HPOCache] = None,
    ):
        self.provider = provider or CompositeHPOProvider()
        self.cache = cache or (
            HPOCache(
                max_size=CONFIG.hpo.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.hpo.CACHE_TTL_SECS,
                disk_path=CONFIG.hpo.CACHE_DISK_PATH or None,
            )
            if CONFIG.hpo.CACHE_ENABLED
            else None
        )

    def query_gene(self, gene_symbol: str) -> Dict[str, Any]:
        """Look up HPO gene-phenotype evidence for a gene symbol directly (used by callers that already know the gene, e.g. tests/reports)."""
        if not CONFIG.hpo.ENABLED:
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
        Look up HPO gene-phenotype evidence for the gene a variant
        overlaps. Never raises -- matches `ClinGenLookup.query_variant`'s
        contract. Takes the already-resolved gene symbol directly
        (see this module's docstring) rather than a `Variant`, since
        HPO has no coordinate-based resolution of its own.
        """
        if not CONFIG.hpo.ENABLED:
            return dict(_SKIPPED_RESULT)

        if not gene_symbol_hint:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "error": None,
                "reason": "no overlapping gene annotation was resolved for this position; HPO annotation is gene-level and requires a resolved gene symbol",
            }

        result = self.query_gene(gene_symbol_hint)
        result.setdefault("gene_symbol", normalize_gene_symbol(gene_symbol_hint))
        return result

    def query_variants_batch(self, gene_symbols: List[Optional[str]]) -> List[Dict[str, Any]]:
        """Batch lookup for an up-front prefetch pass over a whole VCF's already-resolved gene symbols, mirroring `ClinGenLookup.query_variants_batch`."""
        if not CONFIG.hpo.ENABLED:
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
                results.append(
                    {
                        "skipped": False,
                        "found": False,
                        "gene_symbol": None,
                        "reason": "no overlapping gene annotation resolved",
                    }
                )
                continue
            result = (
                cached_by_gene.get(gene_symbol)
                or fetched_by_gene.get(gene_symbol)
                or {"skipped": False, "found": False}
            )
            result = dict(result)
            result.setdefault("gene_symbol", gene_symbol)
            results.append(result)

        return results
