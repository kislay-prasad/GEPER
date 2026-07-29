"""
pipeline/omim/lookup.py
───────────────────────
OMIM gene-disease association lookup.

Supports two backends:
  LOCAL  — parses a local genemap2.txt file
  API    — queries the OMIM REST API

Usage::

    from pipeline.omim.lookup import OmimLookup

    omim = OmimLookup(cfg={"omim": {"genemap_path": "/data/genemap2.txt"}})
    entry = omim.lookup_gene("BRCA1")
    print(entry.mim_number, entry.phenotypes)
    print(omim.is_lof_intolerant("BRCA1"))
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from pipeline.utils.http import _api_get

logger = logging.getLogger("geper.pipeline.omim.lookup")

# ── Hardcoded missense-mechanism genes ────────────────────────────────────────
_MISSENSE_MECHANISM_GENES = frozenset({
    "KCNQ1", "KCNH2", "SCN5A", "BRCA1", "BRCA2", "TP53", "PTEN", "RET",
    "MEN1", "VHL", "MLH1", "MSH2", "APC", "NF1", "NF2", "TSC1", "TSC2",
    "MYBPC3", "MYH7", "LDLR",
})


@dataclass
class OmimGeneEntry:
    """One OMIM gene record."""
    symbol: str
    mim_number: str
    phenotypes: str
    comments: str


class OmimLookup:
    """Look up OMIM gene-disease information.

    Args:
        cfg: Full pipeline config dict. Reads ``cfg["omim"]``.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        om_cfg: Dict = (cfg or {}).get("omim", {}) or {}
        self._genemap_path: Optional[str] = om_cfg.get("genemap_path") or None
        self._api_key: Optional[str] = om_cfg.get("api_key") or None

        # Case-insensitive dict: lowercase symbol → OmimGeneEntry
        self._by_symbol: Dict[str, OmimGeneEntry] = {}
        self._backend = "api"
        # ── Per-run in-memory cache ──
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        import os
        if self._genemap_path and os.path.isfile(self._genemap_path):
            try:
                self._load_genemap(self._genemap_path)
                self._backend = "local"
                logger.info(
                    "[OMIM] Loaded local genemap2: %s (%d genes)",
                    self._genemap_path, len(self._by_symbol),
                )
            except Exception as exc:
                logger.warning("[OMIM] Failed to load genemap2: %s — falling back to API", exc)
                self._backend = "api"

    # ── Local loading ─────────────────────────────────────────────────────────

    def _load_genemap(self, path: str) -> None:
        """Parse genemap2.txt into self._by_symbol."""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            header = None
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("#"):
                    # The last commented line is usually the header
                    if "\t" in line:
                        header = [c.strip() for c in line.lstrip("#").split("\t")]
                    continue
                if header is None:
                    continue
                parts = line.split("\t")
                row = dict(zip(header, parts))

                symbol = (
                    row.get("Approved Gene Symbol")
                    or row.get("Gene Symbols")
                    or ""
                ).strip()
                if not symbol:
                    continue

                mim = (row.get("MIM Number") or "").strip()
                phenotypes = (row.get("Phenotypes") or "").strip()
                comments = (row.get("Comments") or "").strip()

                entry = OmimGeneEntry(
                    symbol=symbol,
                    mim_number=mim,
                    phenotypes=phenotypes,
                    comments=comments,
                )
                self._by_symbol[symbol.lower()] = entry

    # ── Public API ────────────────────────────────────────────────────────────

    def clear_cache(self) -> None:
        """Reset the in-memory lookup cache."""
        with self._cache_lock:
            self._cache.clear()
            self._cache_hits = 0
            self._cache_misses = 0

    def cache_stats(self) -> dict:
        """Return cache hit/miss statistics."""
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "size": len(self._cache),
        }

    def lookup_gene(self, symbol: str) -> Optional[OmimGeneEntry]:
        """Look up an OMIM entry by gene symbol (case-insensitive).

        Returns ``None`` if not found or no API key available.
        """
        cache_key = symbol.lower()
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1

        if self._backend == "local":
            result = self._by_symbol.get(cache_key)
        else:
            result = self._api_lookup_gene(symbol)

        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def is_lof_intolerant(self, symbol: str) -> bool:
        """Return True if this gene is likely haploinsufficiency / LoF intolerant.

        Determined by checking for "autosomal dominant", "haploinsufficiency",
        or inheritance code "(3)" paired with a dominant disorder in Phenotypes.
        """
        entry = self.lookup_gene(symbol)
        if entry is None:
            return False
        ph = entry.phenotypes.lower()
        if "autosomal dominant" in ph:
            return True
        if "haploinsufficiency" in ph:
            return True
        # Phenotype MIM code (3) means molecular basis known + check dominant keyword
        if "(3)" in ph and ("dominant" in ph or "haploinsuffic" in ph):
            return True
        return False

    def is_missense_mechanism(self, symbol: str) -> bool:
        """Return True if missense variants are a known pathogenic mechanism.

        Checks hardcoded gene set first, then OMIM Phenotypes for "missense".
        """
        if symbol.upper() in _MISSENSE_MECHANISM_GENES:
            return True
        entry = self.lookup_gene(symbol)
        if entry and "missense" in entry.phenotypes.lower():
            return True
        return False

    # ── API backend ───────────────────────────────────────────────────────────

    def _api_lookup_gene(self, symbol: str) -> Optional[OmimGeneEntry]:
        """Query OMIM API for a gene by symbol."""
        if not self._api_key:
            logger.warning("[OMIM API] No API key configured — cannot query for '%s'", symbol)
            return None

        search_url = "https://api.omim.org/api/entry/search"
        params = {
            "search": f"gene:{symbol}",
            "retrieve": "geneMap",
            "format": "json",
            "apiKey": self._api_key,
        }
        try:
            resp = _api_get(search_url, params=params, timeout=15, logger=logger)
            if resp is None:
                logger.warning("[OMIM API] Search returned None for gene '%s'", symbol)
                return None
            resp.raise_for_status()
            data = resp.json()
            entries = (
                data.get("omim", {})
                    .get("searchResponse", {})
                    .get("entryList", [])
            )
        except Exception as exc:
            logger.warning("[OMIM API] Search failed for gene '%s': %s", symbol, exc)
            return None

        for item in entries:
            entry_data = item.get("entry", {})
            mim = str(entry_data.get("mimNumber", ""))
            gene_map = entry_data.get("geneMap", {})
            gene_symbol = gene_map.get("approvedGeneSymbol") or symbol
            phenotypes_list = gene_map.get("phenotypeMapList", [])
            phenotype_strs = []
            for p in phenotypes_list:
                pm = p.get("phenotypeMap", {})
                pname = pm.get("phenotype", "")
                inheritance = pm.get("phenotypeMappingKey", "")
                phenotype_strs.append(f"{pname} ({inheritance})")
            phenotypes = "; ".join(phenotype_strs)
            comments = gene_map.get("comments", "")
            return OmimGeneEntry(
                symbol=gene_symbol,
                mim_number=mim,
                phenotypes=phenotypes,
                comments=comments,
            )

        return None
