"""
ConservationLookup: the facade `pipeline/orchestrator.py` talks to,
exactly the same shape as `pipeline.gnomad.lookup.GnomadLookup` -- one
`query_variant(variant, assembly=...)` call that returns a plain
dict, never raises, and is safe to call once per variant inside the
main per-variant loop.

Layering: ConservationLookup -> ConservationCache ->
CompositeConservationProvider -> (LocalBigWigProvider | UCSCApiProvider).
Cache key is position-only (chrom+pos+build, see
pipeline/conservation/utils.py::position_key) since conservation
scores don't depend on the alt allele -- multiple ALT alleles at the
same position share one cache entry.
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.conservation.cache import ConservationCache
from pipeline.conservation.provider import CompositeConservationProvider
from pipeline.conservation.utils import normalize_build, position_key
from pipeline.vcf_parser import Variant
from utils.logger import get_logger

logger = get_logger(__name__)


class ConservationLookup:
    """High-level, cached conservation-score evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeConservationProvider] = None,
        cache: Optional[ConservationCache] = None,
    ):
        self.provider = provider or CompositeConservationProvider()
        self.cache = cache or (
            ConservationCache(
                max_size=CONFIG.conservation.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.conservation.CACHE_TTL_SECS,
                disk_path=CONFIG.conservation.CACHE_DISK_PATH or None,
            )
            if CONFIG.conservation.CACHE_ENABLED
            else None
        )

    def query_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        """Look up conservation evidence for one variant's position. Never raises --
        matches GnomadLookup.query_variant's contract."""
        if not CONFIG.conservation.ENABLED:
            return {
                "skipped": True,
                "reason": "conservation integration disabled via GEPER_ENABLE_CONSERVATION=false",
                "found": False,
            }

        build = normalize_build(assembly)
        key = position_key(variant.chrom, variant.pos, build)

        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                result["skipped"] = False
                return result

        annotation = self.provider.query(variant.chrom, variant.pos, variant.ref, variant.alt, build)
        result = annotation.to_dict()
        result["skipped"] = False

        if self.cache is not None and annotation.error is None:
            self.cache.put(key, result)

        return result

    def query_variants_batch(self, variants: List[Variant], assembly: Optional[str] = None) -> List[Dict[str, Any]]:
        """Synchronous batch lookup (thread-pooled), for an up-front prefetch pass over a whole VCF."""
        if not CONFIG.conservation.ENABLED:
            return [
                {
                    "skipped": True,
                    "reason": "conservation integration disabled via GEPER_ENABLE_CONSERVATION=false",
                    "found": False,
                }
                for _ in variants
            ]
        build = normalize_build(assembly)
        to_fetch: List[Variant] = []
        results: Dict[int, Dict[str, Any]] = {}

        for idx, variant in enumerate(variants):
            key = position_key(variant.chrom, variant.pos, build)
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
                    key = position_key(variant.chrom, variant.pos, build)
                    self.cache.put(key, result)

        return [results[i] for i in range(len(variants))]
