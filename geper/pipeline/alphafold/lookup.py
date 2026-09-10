"""
AlphaFoldLookup: the facade `pipeline/orchestrator.py` talks to, mirroring
`pipeline.interpro.lookup.InterProLookup`'s shape -- keyed by UniProt
accession (resolved by the UniProt stage that runs before this one).

Caching note: `query_accession` (no residue position) is the cached
path -- its result (found/not-found/error, model URLs, version, mean
pLDDT) is accession-level and safe to reuse across every variant
mapping to the same protein. `query_variant`, which additionally
reports the pLDDT *at a specific estimated residue*, is deliberately
NOT served from that same disk cache (a per-position value cached
under an accession-only key would silently return the wrong variant's
residue confidence to the next). It instead calls the provider
directly each time; the provider's own structure-file download is
itself cached in-memory per accession
(`LiveAPIAlphaFoldProvider._structure_cache`), so repeated variants in
the same gene still cost one network fetch, not one per variant.

Layering: AlphaFoldLookup -> AlphaFoldCache (accession-level only) ->
CompositeAlphaFoldProvider -> (LocalDatasetAlphaFoldProvider |
LiveAPIAlphaFoldProvider).
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.alphafold.cache import AlphaFoldCache
from pipeline.alphafold.models import AlphaFoldAnnotation
from pipeline.alphafold.provider import CompositeAlphaFoldProvider
from pipeline.alphafold.utils import accession_cache_key, normalize_accession
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "AlphaFold integration disabled via GEPER_ENABLE_ALPHAFOLD=false",
    "found": False,
}


class AlphaFoldLookup:
    """High-level, cached AlphaFold DB structural-evidence lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeAlphaFoldProvider] = None,
        cache: Optional[AlphaFoldCache] = None,
    ):
        self.provider = provider or CompositeAlphaFoldProvider()
        self.cache = cache or (
            AlphaFoldCache(
                max_size=CONFIG.alphafold.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.alphafold.CACHE_TTL_SECS,
                disk_path=CONFIG.alphafold.CACHE_DISK_PATH or None,
            )
            if CONFIG.alphafold.CACHE_ENABLED
            else None
        )

    def query_accession(self, accession: str) -> Dict[str, Any]:
        """Look up accession-level (position-independent) AlphaFold DB evidence for a UniProt accession. Cached."""
        if not CONFIG.alphafold.ENABLED:
            return dict(_SKIPPED_RESULT)

        accession = normalize_accession(accession)
        if not accession:
            return {"skipped": False, "found": False, "error": "no UniProt accession provided"}

        key = accession_cache_key(accession)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        annotation = self.provider.query(accession, protein_position=None)
        result = annotation.to_dict()
        result["skipped"] = False

        if self.cache is not None and annotation.error is None:
            self.cache.put(key, result)

        return result

    def query_variant(
        self,
        uniprot_result: Optional[Dict[str, Any]],
        protein_position: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Look up AlphaFold DB structural evidence for one variant, given
        the UniProt stage's result for the same variant. Never raises
        -- matches `InterProLookup.query_variant`'s contract. See this
        module's docstring for why the position-specific result is not
        served from `AlphaFoldCache`.
        """
        if not CONFIG.alphafold.ENABLED:
            return dict(_SKIPPED_RESULT)

        accession = normalize_accession((uniprot_result or {}).get("accession"))
        if not accession:
            return {
                "skipped": False,
                "found": False,
                "accession": None,
                "error": None,
                "reason": "no UniProt accession available for this variant's gene; AlphaFold DB annotation requires one",
            }

        if protein_position is None:
            result = self.query_accession(accession)
            result.setdefault("accession", accession)
            return result

        try:
            annotation = self.provider.query(accession, protein_position=protein_position)
        except Exception as exc:  # noqa: BLE001 - defense-in-depth; provider already guards internally
            logger.error(f"AlphaFold lookup raised unexpectedly for '{accession}': {exc}")
            annotation = AlphaFoldAnnotation.from_error(accession, f"unexpected error: {exc}")

        result = annotation.to_dict()
        result["skipped"] = False
        result.setdefault("accession", accession)
        return result

    def query_accessions_batch(self, accessions: List[str]) -> List[Dict[str, Any]]:
        """Batch (accession-level, position-independent) lookup for an up-front prefetch pass over many distinct accessions."""
        if not CONFIG.alphafold.ENABLED:
            return [dict(_SKIPPED_RESULT) for _ in accessions]
        return [self.query_accession(a) for a in accessions]
