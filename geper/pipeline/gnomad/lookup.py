"""
GnomadLookup: the facade `pipeline/orchestrator.py` talks to, in
exactly the same shape as `database.clinvar_client.ClinVarClient` /
`database.dbsnp_client.DbSNPClient` -- one `query_variant(variant,
assembly=...)` call that returns a plain dict, never raises, and is
safe to call once per variant inside the main per-variant loop.

Layering: GnomadLookup -> GnomadCache -> CompositeGnomadProvider ->
(LocalIndexedGnomadProvider | GraphQLGnomadProvider). Batch/async
methods are exposed for callers (e.g. an up-front prefetch pass,
mirroring `database/blast_client.py`'s prefetch pattern) that want to
resolve many variants concurrently instead of one at a time.
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.gnomad.cache import GnomadCache
from pipeline.gnomad.provider import CompositeGnomadProvider
from pipeline.gnomad.utils import normalize_build, variant_key
from pipeline.vcf_parser import Variant
from utils.logger import get_logger

logger = get_logger(__name__)


class GnomadLookup:
    """High-level, cached gnomAD evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeGnomadProvider] = None,
        cache: Optional[GnomadCache] = None,
    ):
        self.provider = provider or CompositeGnomadProvider()
        self.cache = cache or (
            GnomadCache(
                max_size=CONFIG.gnomad.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.gnomad.CACHE_TTL_SECS,
                disk_path=CONFIG.gnomad.CACHE_DISK_PATH or None,
            )
            if CONFIG.gnomad.CACHE_ENABLED
            else None
        )

    def query_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        """
        Look up gnomAD evidence for one variant. Never raises --
        matches `ClinVarClient.query_variant` / `DbSNPClient.lookup_variant`'s
        contract so the orchestrator's per-stage try/except wrapper
        (`_run_gnomad_stage`) only ever needs to guard against this
        function returning a result whose `error` field is set, not
        against it throwing.
        """
        if not CONFIG.gnomad.ENABLED:
            return {
                "skipped": True,
                "reason": "gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false",
                "found": False,
            }

        build = normalize_build(assembly)
        key = variant_key(variant.chrom, variant.pos, variant.ref, variant.alt, build)

        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        annotation = self.provider.query(variant.chrom, variant.pos, variant.ref, variant.alt, build)
        result = annotation.to_dict()
        result["skipped"] = False

        if self.cache is not None and annotation.error is None:
            # Only cache clean results (found or genuinely not-found) --
            # a transient network/tabix error should be retried on the
            # next run, not pinned into the cache for CACHE_TTL_SECS.
            self.cache.put(key, result)

        return result

    def query_variants_batch(self, variants: List[Variant], assembly: Optional[str] = None) -> List[Dict[str, Any]]:
        """Synchronous batch lookup (thread-pooled), for an up-front prefetch pass over a whole VCF."""
        if not CONFIG.gnomad.ENABLED:
            return [
                {"skipped": True, "reason": "gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false", "found": False}
                for _ in variants
            ]
        build = normalize_build(assembly)
        to_fetch: List[Variant] = []
        results: Dict[int, Dict[str, Any]] = {}

        for idx, variant in enumerate(variants):
            key = variant_key(variant.chrom, variant.pos, variant.ref, variant.alt, build)
            cached = self.cache.get(key) if self.cache is not None else None
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                result["skipped"] = False
                results[idx] = result
            else:
                to_fetch.append((idx, variant))

        if to_fetch:
            fetched = self.provider.batch_query([(v.chrom, v.pos, v.ref, v.alt, build) for _, v in to_fetch])
            for (idx, variant), annotation in zip(to_fetch, fetched):
                result = annotation.to_dict()
                result["skipped"] = False
                results[idx] = result
                if self.cache is not None and annotation.error is None:
                    key = variant_key(variant.chrom, variant.pos, variant.ref, variant.alt, build)
                    self.cache.put(key, result)

        return [results[i] for i in range(len(variants))]

    async def async_query_variants_batch(
        self, variants: List[Variant], assembly: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Async equivalent of `query_variants_batch`, for callers already running inside an event loop (e.g. an async API server)."""
        if not CONFIG.gnomad.ENABLED:
            return [
                {"skipped": True, "reason": "gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false", "found": False}
                for _ in variants
            ]
        build = normalize_build(assembly)
        annotations = await self.provider.async_batch_query([(v.chrom, v.pos, v.ref, v.alt, build) for v in variants])
        results = []
        for variant, annotation in zip(variants, annotations):
            result = annotation.to_dict()
            result["skipped"] = False
            results.append(result)
            if self.cache is not None and annotation.error is None:
                key = variant_key(variant.chrom, variant.pos, variant.ref, variant.alt, build)
                self.cache.put(key, result)
        return results
