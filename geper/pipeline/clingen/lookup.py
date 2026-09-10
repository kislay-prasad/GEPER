"""
ClinGenLookup: the facade `pipeline/orchestrator.py` talks to, in the
same shape as `pipeline.gnomad.lookup.GnomadLookup` -- one
`query_variant(variant, assembly=...)` call that returns a plain dict,
never raises, and is safe to call once per variant inside the main
per-variant loop.

The extra step ClinGen needs that gnomAD doesn't: ClinGen curation is
gene-level, so a variant must first be mapped to a gene symbol (via
`pipeline.clingen.utils.resolve_gene_symbol`, an Ensembl overlap/region
lookup) before any evidence can be fetched. That gene-symbol
resolution is itself cached (a variant's overlapping gene never
changes between runs), separately from the gene-evidence cache, so a
VCF with many variants in the same gene (e.g. exon-by-exon coverage of
one disease gene) pays the Ensembl round trip once per distinct
position, and the ClinGen evidence fetch once per distinct gene.

Layering: ClinGenLookup -> ClinGenCache -> CompositeClinGenProvider ->
(LocalDatasetClinGenProvider | LiveAPIClinGenProvider).
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.clingen.cache import ClinGenCache
from pipeline.clingen.provider import CompositeClinGenProvider
from pipeline.clingen.utils import (
    GeneResolution,
    GeneResolutionStatus,
    gene_cache_key,
    normalize_gene_symbol,
    resolve_gene_symbol_detail,
)
from pipeline.vcf_parser import Variant
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "ClinGen integration disabled via GEPER_ENABLE_CLINGEN=false",
    "found": False,
}


class ClinGenLookup:
    """High-level, cached ClinGen gene-evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeClinGenProvider] = None,
        cache: Optional[ClinGenCache] = None,
    ):
        self.provider = provider or CompositeClinGenProvider()
        self.cache = cache or (
            ClinGenCache(
                max_size=CONFIG.clingen.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.clingen.CACHE_TTL_SECS,
                disk_path=CONFIG.clingen.CACHE_DISK_PATH or None,
            )
            if CONFIG.clingen.CACHE_ENABLED
            else None
        )
        # A small in-process memo of chrom:pos -> GeneResolution, kept
        # separate from the (persisted, TTL'd) gene-evidence cache
        # above: gene-boundary annotation doesn't need a TTL or disk
        # persistence to be safe to reuse for the life of one process.
        self._gene_symbol_memo: Dict[str, GeneResolution] = {}

    def _resolve_gene(self, variant: Variant, build: str) -> GeneResolution:
        """
        Full-detail resolution (see `pipeline/clingen/utils.py`'s
        docstring) -- prefers the VCF's own `GENE=` INFO field when
        present, only falling back to the Ensembl overlap/region
        lookup (with CDS-containment/MANE-Select disambiguation for a
        genuinely overlapping-gene position) when the VCF carries no
        such annotation.
        """
        memo_key = f"{build}:{variant.chrom}:{variant.pos}"
        if memo_key in self._gene_symbol_memo:
            return self._gene_symbol_memo[memo_key]
        info = {k.upper(): v for k, v in (variant.info or {}).items()}
        vcf_gene_hint = info.get("GENE")
        resolution = resolve_gene_symbol_detail(variant.chrom, variant.pos, build=build, vcf_gene_hint=vcf_gene_hint)
        self._gene_symbol_memo[memo_key] = resolution
        return resolution

    def query_gene(self, gene_symbol: str) -> Dict[str, Any]:
        """Look up ClinGen evidence for a gene symbol directly (used by callers that already know the gene, e.g. tests/reports)."""
        if not CONFIG.clingen.ENABLED:
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

    def query_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        """
        Look up ClinGen evidence for one variant. Never raises --
        matches `GnomadLookup.query_variant`'s contract so the
        orchestrator's per-stage try/except wrapper only ever needs to
        guard against this function returning a result whose `error`
        field is set, not against it throwing.
        """
        if not CONFIG.clingen.ENABLED:
            return dict(_SKIPPED_RESULT)

        build = assembly or "GRCh38"
        resolution = self._resolve_gene(variant, build)
        if not resolution.gene_symbol:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "error": None,
                "reason": (
                    "gene resolution ambiguous; ClinGen curation is gene-level and refuses to guess "
                    f"among tied candidates: {resolution.reason}"
                    if resolution.status == GeneResolutionStatus.AMBIGUOUS
                    else f"no overlapping gene annotation found for this position; ClinGen curation is gene-level and requires a resolved gene symbol ({resolution.reason})"
                ),
                "gene_resolution_status": resolution.status.value,
                "gene_resolution_candidates": resolution.candidates,
            }

        result = self.query_gene(resolution.gene_symbol)
        result.setdefault("gene_symbol", resolution.gene_symbol)
        result["gene_resolution_status"] = resolution.status.value
        result["gene_resolution_source"] = resolution.source
        return result

    def query_variants_batch(self, variants: List[Variant], assembly: Optional[str] = None) -> List[Dict[str, Any]]:
        """Batch lookup for an up-front prefetch pass over a whole VCF, mirroring `GnomadLookup.query_variants_batch`."""
        if not CONFIG.clingen.ENABLED:
            return [dict(_SKIPPED_RESULT) for _ in variants]

        build = assembly or "GRCh38"
        resolutions = [self._resolve_gene(v, build) for v in variants]

        # Resolve each *distinct* gene once, then fan the shared
        # per-gene result back out to every variant that mapped to it
        # -- avoids re-querying ClinGen once per variant for genes
        # covered by many variants in the same VCF.
        distinct_genes = sorted({r.gene_symbol for r in resolutions if r.gene_symbol})
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
        for resolution in resolutions:
            if not resolution.gene_symbol:
                results.append(
                    {
                        "skipped": False,
                        "found": False,
                        "gene_symbol": None,
                        "reason": (
                            "gene resolution ambiguous; ClinGen curation is gene-level and refuses to "
                            f"guess among tied candidates: {resolution.reason}"
                            if resolution.status == GeneResolutionStatus.AMBIGUOUS
                            else f"no overlapping gene annotation found for this position ({resolution.reason})"
                        ),
                        "gene_resolution_status": resolution.status.value,
                        "gene_resolution_candidates": resolution.candidates,
                    }
                )
                continue
            gene_symbol = resolution.gene_symbol
            result = (
                cached_by_gene.get(gene_symbol)
                or fetched_by_gene.get(gene_symbol)
                or {"skipped": False, "found": False}
            )
            result = dict(result)
            result.setdefault("gene_symbol", gene_symbol)
            result["gene_resolution_status"] = resolution.status.value
            result["gene_resolution_source"] = resolution.source
            results.append(result)

        return results
