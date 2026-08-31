"""
InterProLookup: the facade `pipeline/orchestrator.py` talks to, mirroring
`pipeline.uniprot.lookup.UniProtLookup`'s shape -- one
`query_accession(accession)` / `query_variant(variant, uniprot_result,
protein_position=...)` call that returns a plain dict, never raises.

InterPro/Pfam domain calls are keyed by UniProt accession, not by gene
symbol or genomic coordinate -- so this stage runs immediately after
the UniProt stage (per the integration order in the project spec) and
takes that stage's resolved accession as input, rather than re-deriving
it. When no accession is available (UniProt lookup skipped, disabled,
or found nothing), this is reported as `"skipped"`/not found rather
than attempting a lookup with no key.

Layering: InterProLookup -> InterProCache -> CompositeInterProProvider
-> (LocalDatasetInterProProvider | LiveAPIInterProProvider).
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.interpro.cache import InterProCache
from pipeline.interpro.provider import CompositeInterProProvider
from pipeline.interpro.utils import accession_cache_key, normalize_accession
from utils.logger import get_logger

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "InterPro integration disabled via GEPER_ENABLE_INTERPRO=false",
    "found": False,
}

# PM1 ("located in a mutational hot spot and/or critical and
# well-established functional domain") means a structural/functional
# sub-region, not gene-family membership. InterPro's "family" entries
# routinely span nearly a protein's entire length (e.g. BRCA1's
# IPR011364 "Breast cancer type 1 susceptibility protein" covers aa
# 55-1794 of a 1863-residue protein) -- verified against live BRCA1/
# TP53 data in tests/test_pm1_interpro.py. Counting those as "domain
# overlap" would make PM1 fire for almost any residue in a
# well-characterized gene, not just genuine domain/site regions, so
# they're excluded here even though `domains` (the unfiltered list)
# still reports them for completeness.
_NON_DOMAIN_ENTRY_TYPES = {"family"}


class InterProLookup:
    """High-level, cached InterPro/Pfam domain-annotation lookup used by the orchestrator."""

    def __init__(
        self,
        provider: Optional[CompositeInterProProvider] = None,
        cache: Optional[InterProCache] = None,
    ):
        self.provider = provider or CompositeInterProProvider()
        self.cache = cache or (
            InterProCache(
                max_size=CONFIG.interpro.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.interpro.CACHE_TTL_SECS,
                disk_path=CONFIG.interpro.CACHE_DISK_PATH or None,
            )
            if CONFIG.interpro.CACHE_ENABLED
            else None
        )

    def query_accession(self, accession: str) -> Dict[str, Any]:
        """Look up InterPro/Pfam domain evidence for a UniProt accession directly."""
        if not CONFIG.interpro.ENABLED:
            return dict(_SKIPPED_RESULT)

        normalized_accession = normalize_accession(accession)
        if not normalized_accession:
            return {"skipped": False, "found": False, "error": "no UniProt accession provided"}
        accession = normalized_accession

        key = accession_cache_key(accession)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                return result

        annotation = self.provider.query(accession)
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
        Look up InterPro/Pfam domain evidence for one variant, given
        the UniProt stage's result for the same variant. Never raises
        -- matches `UniProtLookup.query_variant`'s contract.

        `protein_position`, when provided, is used to annotate which
        returned domains overlap the variant (`affected_domains` in
        the result) -- see the orchestrator's
        `_canonical_protein_position` for how it's derived (a
        transcript-verified canonical residue number, or `None` when
        it genuinely could not be determined).

        `affected_domains` is deliberately three-valued, not just
        list-or-empty: `None` means "domain overlap was never checked"
        (no position was available -- `protein_position` is also
        `None` in the result), `[]` means "checked, no annotated
        domain overlaps this exact residue", and a non-empty list
        means a real overlap. Collapsing the first two into the same
        `[]` (as this used to do) made a missing position
        indistinguishable from a genuine negative -- `_pm1` in
        `acmg_rules.py` depends on being able to tell them apart to
        report `not_evaluated` instead of a confident-but-wrong
        `not_triggered`.
        """
        if not CONFIG.interpro.ENABLED:
            return dict(_SKIPPED_RESULT)

        accession = normalize_accession((uniprot_result or {}).get("accession"))
        if not accession:
            return {
                "skipped": False,
                "found": False,
                "accession": None,
                "error": None,
                "reason": "no UniProt accession available for this variant's gene; InterPro/Pfam annotation requires one",
            }

        result = self.query_accession(accession)
        result.setdefault("accession", accession)
        result["protein_position"] = protein_position

        # INERT (2026-08-31): truthiness on `error`, not `is not None`,
        # but `result.get("found")` immediately to its left already
        # requires True, and a failed `query_accession` call always
        # returns found=False alongside its error -- so this
        # sub-condition never changes which branch fires. Fixed to
        # `is not None` for consistency only.
        if protein_position is not None and result.get("found") and result.get("error") is None:
            domains = result.get("domains") or []
            affected = [
                d
                for d in domains
                if isinstance(d.get("start"), int)
                and isinstance(d.get("end"), int)
                and d["start"] <= protein_position <= d["end"]
                and d.get("type") not in _NON_DOMAIN_ENTRY_TYPES
            ]
            result["affected_domains"] = affected
            result["protein_position_basis"] = "transcript_cds"
        else:
            result["affected_domains"] = None
            result["protein_position_basis"] = None

        return result

    def query_accessions_batch(self, accessions: List[str]) -> List[Dict[str, Any]]:
        """Batch lookup for an up-front prefetch pass over many distinct accessions."""
        if not CONFIG.interpro.ENABLED:
            return [dict(_SKIPPED_RESULT) for _ in accessions]

        normalized_accessions = {normalize_accession(a) for a in accessions}
        distinct = sorted(a for a in normalized_accessions if a)
        to_fetch = []
        cached_by_accession: Dict[str, Dict[str, Any]] = {}
        for accession in distinct:
            key = accession_cache_key(accession)
            cached = self.cache.get(key) if self.cache is not None else None
            if cached is not None:
                result = dict(cached)
                result["source"] = "cache"
                cached_by_accession[accession] = result
            else:
                to_fetch.append(accession)

        fetched_by_accession: Dict[str, Dict[str, Any]] = {}
        if to_fetch:
            fetched = self.provider.batch_query(to_fetch)
            for accession, annotation in zip(to_fetch, fetched):
                result = annotation.to_dict()
                result["skipped"] = False
                fetched_by_accession[accession] = result
                if self.cache is not None and annotation.error is None:
                    self.cache.put(accession_cache_key(accession), result)

        results = []
        for accession in accessions:
            normalized = normalize_accession(accession)
            if not normalized:
                results.append({"skipped": False, "found": False, "accession": None})
                continue
            result = (
                cached_by_accession.get(normalized)
                or fetched_by_accession.get(normalized)
                or {"skipped": False, "found": False}
            )
            result = dict(result)
            result.setdefault("accession", normalized)
            results.append(result)
        return results
