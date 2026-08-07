"""
FunctionalEvidenceLookup: the facade `pipeline/orchestrator.py` talks
to for PS3/BS3 functional-assay evidence.

Unlike ClinGen/HPO/Orphanet (gene-level annotation, answered once per
gene and identical for every variant in that gene), functional
evidence is fundamentally variant-level: "does this exact substitution
behave abnormally in a published assay?" So `query_variant` takes the
variant's own genomic and coding HGVS strings (already computed by the
orchestrator via `pipeline/hgvs_utils.py` -- see
`pipeline/orchestrator.py::_run_functional_evidence_stage`) and looks
them up against a per-gene index built from each source.

Two sources, primary/secondary (not local/API-fallback -- both are
live APIs):
  1. ClinGen Evidence Repository (`erepo_provider.py`) -- tried first.
     A hit here (an already-adjudicated VCEP PS3 or BS3 "Met" call) is
     used as-is; MaveDB is never consulted when ERepo already answered
     this exact variant.
  2. MaveDB (`mavedb_provider.py`) -- tried only when ERepo had nothing
     for this variant (whether because the gene/variant genuinely
     isn't curated there, or because the ERepo request itself failed).
     A raw functional-assay score is bucketed into a PS3/BS3-relevant
     call using that score set's own calibration, never a threshold
     GEPER invents.

Both sources are queried once per *gene*, not per variant -- each
provider's `fetch_gene_index()` returns every curated/assayed variant
for that gene in one pass, cached (`FunctionalEvidenceCache`) so every
subsequent variant in an already-seen gene costs zero additional
network calls for the rest of the run, the same performance shape
`pipeline/clingen/lookup.py` and `pipeline/hpo/lookup.py` already use.

A gene neither source covers at all (confirmed live for CFTR during
development -- see both providers' module docstrings) simply reports
`found: False`; the caller (`ACMGRuleEngine._ps3`/`_bs3`) turns that
into an honest "not_evaluated", never a fabricated call.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.functional_evidence.cache import FunctionalEvidenceCache
from pipeline.functional_evidence.erepo_provider import ErepoFunctionalEvidenceProvider
from pipeline.functional_evidence.mavedb_provider import MaveDBFunctionalEvidenceProvider
from pipeline.functional_evidence.models import FunctionalEvidenceRecord, FunctionalEvidenceResult
from pipeline.functional_evidence.utils import normalize_hgvs_c
from utils.logger import get_logger
from utils.service_health import HEALTH

logger = get_logger(__name__)

_SKIPPED_RESULT = {
    "skipped": True,
    "reason": "Functional-evidence (PS3/BS3) integration disabled via GEPER_ENABLE_FUNCTIONAL_EVIDENCE=false",
    "found": False,
}


class FunctionalEvidenceLookup:
    def __init__(
        self,
        erepo_provider: Optional[ErepoFunctionalEvidenceProvider] = None,
        mavedb_provider: Optional[MaveDBFunctionalEvidenceProvider] = None,
        cache: Optional[FunctionalEvidenceCache] = None,
    ):
        self.erepo_provider = erepo_provider or ErepoFunctionalEvidenceProvider()
        self.mavedb_provider = mavedb_provider or MaveDBFunctionalEvidenceProvider()
        self.cache = cache or (
            FunctionalEvidenceCache(
                max_size=CONFIG.functional_evidence.CACHE_MAX_SIZE,
                ttl_seconds=CONFIG.functional_evidence.CACHE_TTL_SECS,
                disk_path=CONFIG.functional_evidence.CACHE_DISK_PATH or None,
            )
            if CONFIG.functional_evidence.CACHE_ENABLED
            else None
        )

    def query_variant(
        self,
        gene_symbol: Optional[str],
        hgvs_g: Optional[str] = None,
        hgvs_c: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not CONFIG.functional_evidence.ENABLED:
            return dict(_SKIPPED_RESULT)

        if not gene_symbol:
            return {
                "skipped": False,
                "found": False,
                "gene_symbol": None,
                "error": None,
                "records": [],
                "reason": "no gene symbol was resolved for this variant.",
            }

        errors: List[str] = []
        unavailable_sources: List[str] = []

        erepo_records = self._match_erepo(gene_symbol, hgvs_g, errors, unavailable_sources)
        if erepo_records:
            return FunctionalEvidenceResult(
                gene_symbol=gene_symbol,
                source="clingen_erepo",
                found=True,
                records=erepo_records,
            ).to_dict()

        mavedb_record = self._match_mavedb(gene_symbol, hgvs_c, errors, unavailable_sources)
        if mavedb_record:
            return FunctionalEvidenceResult(
                gene_symbol=gene_symbol,
                source="mavedb",
                found=True,
                records=[mavedb_record],
            ).to_dict()

        result = FunctionalEvidenceResult.not_found(gene_symbol, "none")
        if errors:
            result.error = "; ".join(errors)
        if unavailable_sources:
            result.unavailable_sources = unavailable_sources
        return result.to_dict()

    # -- ClinGen ERepo (primary) ----------------------------------------

    def _match_erepo(
        self, gene_symbol: str, hgvs_g: Optional[str], errors: List[str], unavailable_sources: List[str]
    ) -> List[FunctionalEvidenceRecord]:
        if not hgvs_g:
            return []
        if HEALTH.is_offline("ClinGen ERepo"):
            unavailable_sources.append("ClinGen ERepo")
            return []
        try:
            index = self._erepo_gene_index(gene_symbol)
        except Exception as exc:  # noqa: BLE001 - a primary-source failure must still let MaveDB be tried
            logger.warning(f"ClinGen ERepo lookup failed for gene '{gene_symbol}': {exc}")
            errors.append(f"ClinGen ERepo: {exc}")
            return []
        matches = index.get(hgvs_g, [])
        return [dataclasses.replace(r, matched_hgvs=hgvs_g) for r in matches]

    def _erepo_gene_index(self, gene_symbol: str) -> Dict[str, List[FunctionalEvidenceRecord]]:
        cache_key = f"erepo:{gene_symbol.strip().upper()}"
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        if not self.erepo_provider.is_available():
            return {}
        index = self.erepo_provider.fetch_gene_index(gene_symbol)
        if self.cache:
            self.cache.put(cache_key, index)
        return index

    # -- MaveDB (secondary) ----------------------------------------------

    def _match_mavedb(
        self, gene_symbol: str, hgvs_c: Optional[str], errors: List[str], unavailable_sources: List[str]
    ) -> Optional[FunctionalEvidenceRecord]:
        key = normalize_hgvs_c(hgvs_c)
        if key is None:
            return None
        if HEALTH.is_offline("MaveDB"):
            unavailable_sources.append("MaveDB")
            return None
        try:
            index = self._mavedb_gene_index(gene_symbol)
        except Exception as exc:  # noqa: BLE001 - secondary source, must never be fatal
            logger.warning(f"MaveDB lookup failed for gene '{gene_symbol}': {exc}")
            errors.append(f"MaveDB: {exc}")
            return None
        match = index.get(key)
        if match is None:
            return None
        return dataclasses.replace(match, matched_hgvs=hgvs_c)

    def _mavedb_gene_index(self, gene_symbol: str) -> Dict[str, FunctionalEvidenceRecord]:
        cache_key = f"mavedb:{gene_symbol.strip().upper()}"
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        if not self.mavedb_provider.is_available():
            return {}
        index = self.mavedb_provider.fetch_gene_index(gene_symbol)
        if self.cache:
            self.cache.put(cache_key, index)
        return index
