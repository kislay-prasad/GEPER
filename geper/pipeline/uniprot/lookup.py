"""
UniProtLookup: the facade `pipeline/orchestrator.py` talks to, in the
same shape as `pipeline.gnomad.lookup.GnomadLookup` /
`pipeline.clingen.lookup.ClinGenLookup` -- one `query_variant(variant,
assembly=..., gene_symbol_hint=...)` call that returns a plain dict,
never raises, and is safe to call once per variant.

UniProt annotation, like ClinGen curation, is gene-level: a variant
must first be mapped to a gene symbol. Rather than repeating the
Ensembl overlap/region lookup `pipeline.clingen.utils.resolve_gene_symbol`
already performs for the ClinGen stage, `query_variant` accepts an
optional `gene_symbol_hint` so the orchestrator can pass through the
gene symbol ClinGen's stage already resolved for the same variant --
avoiding a second network round trip for gene resolution. When no hint
is supplied (e.g. a standalone caller/test), this falls back to
resolving the gene itself via the same Ensembl helper.

Layering: UniProtLookup -> UniProtCache -> CompositeUniProtProvider ->
(LocalDatasetUniProtProvider | LiveAPIUniProtProvider).
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.clingen.utils import resolve_gene_symbol
from pipeline.uniprot.cache import UniProtCache
from pipeline.uniprot.models import UniProtAnnotation
from pipeline.uniprot.provider import CompositeUniProtProvider
from pipeline.uniprot.utils import gene_cache_key, normalize_gene_symbol
from pipeline.vcf_parser import Variant
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {"skipped": True, "reason": "UniProt integration disabled via GEPER_ENABLE_UNIPROT=false", "found": False}


class UniProtLookup:
    """High-level, cached UniProt gene-annotation lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeUniProtProvider] = None,
        cache: Optional[UniProtCache] = None,
    ):
        self.provider = provider or CompositeUniProtProvider()
        self.cache = cache or (
            UniProtCache(
                max_size=CONFIG.uniprot.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.uniprot.CACHE_TTL_SECS,
                disk_path=CONFIG.uniprot.CACHE_DISK_PATH or None,
            )
            if CONFIG.uniprot.CACHE_ENABLED
            else None
        )
        # Same rationale as ClinGenLookup._gene_symbol_memo: a
        # non-persisted, non-TTL'd memo since gene-boundary annotation
        # doesn't change within one process's lifetime.
        self._gene_symbol_memo: Dict[str, Optional[str]] = {}

    def _resolve_gene(self, variant: Variant, build: str) -> Optional[str]:
        memo_key = f"{build}:{variant.chrom}:{variant.pos}"
        if memo_key in self._gene_symbol_memo:
            return self._gene_symbol_memo[memo_key]
        gene_symbol = resolve_gene_symbol(variant.chrom, variant.pos, build=build)
        self._gene_symbol_memo[memo_key] = gene_symbol
        return gene_symbol

    def query_gene(self, gene_symbol: str) -> Dict[str, Any]:
        """Look up UniProt evidence for a gene symbol directly (used by callers that already know the gene, e.g. tests/reports)."""
        if not CONFIG.uniprot.ENABLED:
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

        annotation = self.provider.query(gene_symbol)
        result = annotation.to_dict()
        result["skipped"] = False

        if self.cache is not None and annotation.error is None:
            self.cache.put(key, result)

        return result

    def query_variant(
        self, variant: Variant, assembly: Optional[str] = None, gene_symbol_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Look up UniProt evidence for one variant. Never raises --
        matches `GnomadLookup.query_variant` / `ClinGenLookup.query_variant`'s
        contract. `gene_symbol_hint`, when provided (e.g. the gene
        symbol ClinGen's stage already resolved for this variant),
        skips this module's own gene-resolution call entirely.
        """
        if not CONFIG.uniprot.ENABLED:
            return dict(_SKIPPED_RESULT)

        build = assembly or "GRCh38"
        gene_symbol = normalize_gene_symbol(gene_symbol_hint) or self._resolve_gene(variant, build)
        if not gene_symbol:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "error": None,
                "reason": "no overlapping gene annotation found for this position; UniProt annotation is gene-level and requires a resolved gene symbol",
            }

        result = self.query_gene(gene_symbol)
        result.setdefault("gene_symbol", gene_symbol)
        return result

    def query_variants_batch(
        self, variants: List[Variant], assembly: Optional[str] = None, gene_symbol_hints: Optional[List[Optional[str]]] = None
    ) -> List[Dict[str, Any]]:
        """Batch lookup for an up-front prefetch pass over a whole VCF, mirroring `ClinGenLookup.query_variants_batch`."""
        if not CONFIG.uniprot.ENABLED:
            return [dict(_SKIPPED_RESULT) for _ in variants]

        build = assembly or "GRCh38"
        hints = gene_symbol_hints or [None] * len(variants)
        gene_symbols = [
            normalize_gene_symbol(hint) or self._resolve_gene(variant, build)
            for variant, hint in zip(variants, hints)
        ]

        distinct_genes = sorted({g for g in gene_symbols if g})
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
            for gene, annotation in zip(to_fetch, fetched):
                result = annotation.to_dict()
                result["skipped"] = False
                fetched_by_gene[gene] = result
                if self.cache is not None and annotation.error is None:
                    self.cache.put(gene_cache_key(gene), result)

        results: List[Dict[str, Any]] = []
        for gene_symbol in gene_symbols:
            if not gene_symbol:
                results.append(
                    {
                        "skipped": False,
                        "found": False,
                        "gene_symbol": None,
                        "reason": "no overlapping gene annotation found for this position",
                    }
                )
                continue
            result = cached_by_gene.get(gene_symbol) or fetched_by_gene.get(gene_symbol) or {"skipped": False, "found": False}
            result = dict(result)
            result.setdefault("gene_symbol", gene_symbol)
            results.append(result)

        return results
