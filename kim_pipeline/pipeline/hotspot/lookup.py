"""
pipeline/hotspot/lookup.py
───────────────────────────
ClinVar-derived mutational hotspot and UniProt functional domain lookup.

Replaces the 20-gene hardcoded allowlist for PM1 (in_hotspot).

Data sources:
  PRIMARY   — ClinVar variant_summary.txt.gz (already used by ClinVarLookup).
              A genomic position with ≥ min_plp_count independent Pathogenic /
              Likely_Pathogenic missense submissions is classified as a hotspot.
              Zero extra downloads required if ClinVar TSV is already configured.

  SECONDARY — UniProt functional domain BED file (optional).
              Format: chrom  start  end  gene:domain_name  (0-based BED coords)
              Any missense within a UniProt domain also qualifies.
              Config key: hotspot.uniprot_domains_bed

Config::

    hotspot:
      clinvar_tsv_gz_path: /data/variant_summary.txt.gz  # reuse clinvar path
      min_plp_count: 3          # P/LP missense submissions to call a hotspot
      uniprot_domains_bed: /data/uniprot_domains.bed      # optional
      timeout: 20               # API timeout (ClinVar esearch fallback)

Download UniProt domains BED (optional, ~60 MB)::

    # Available from UCSC Table Browser → UniProt track → BED export
    # or from https://ftp.uniprot.org/pub/databases/uniprot/ (custom conversion)

When neither ClinVar TSV nor domain BED is configured, a position is
simply reported as not-a-hotspot (no fallback data source).
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger("geper.pipeline.hotspot.lookup")

# Canonical P/LP significance strings from ClinVar
_PLP_TERMS = frozenset(
    {
        "pathogenic",
        "likely pathogenic",
        "pathogenic/likely pathogenic",
    }
)


def _is_plp(sig: str) -> bool:
    return sig.strip().lower() in _PLP_TERMS


def _is_missense_or_small_indel(variant_type: str) -> bool:
    """Filter to missense and small in-frame variants only.

    ClinVar variant_summary.txt Type field values include:
    'single nucleotide variant', 'Indel', 'Deletion', 'Insertion', ...
    For hotspot purposes we include SNVs and short indels (≤ 9 bp) that are
    likely missense/in-frame, not large structural variants.
    """
    vt = variant_type.strip().lower()
    return vt in (
        "single nucleotide variant",
        "snv",
        "indel",
        "insertion",
        "deletion",
        "duplication",
        "microsatellite",
    )


@dataclass
class HotspotRecord:
    """Hotspot information for a queried position."""

    chrom: str
    pos: int
    plp_count: int  # number of independent P/LP ClinVar submissions
    in_uniprot_domain: bool
    domain_name: str | None
    is_hotspot: bool
    backend_used: str


class HotspotLookup:
    """Classify a genomic position as a mutational hotspot.

    Args:
        cfg: Full pipeline config dict. Reads ``cfg["hotspot"]`` and falls
             back to ``cfg["clinvar"]["tsv_gz_path"]``.
    """

    def __init__(
        self,
        cfg: dict | None = None,
    ) -> None:
        hs_cfg: dict = (cfg or {}).get("hotspot", {}) or {}
        cv_cfg: dict = (cfg or {}).get("clinvar", {}) or {}

        # ClinVar TSV — prefer hotspot-specific key, fall back to clinvar key
        self._clinvar_tsv: str | None = (
            hs_cfg.get("clinvar_tsv_gz_path") or cv_cfg.get("tsv_gz_path") or None
        )
        self._min_plp: int = int(hs_cfg.get("min_plp_count", 3))
        self._domains_bed: str | None = hs_cfg.get("uniprot_domains_bed") or None
        self._timeout: int = int(hs_cfg.get("timeout", 20))
        # ── Per-run in-memory cache ──
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        # (norm_chrom, pos) → P/LP submission count for missense-like variants
        self._plp_counts: dict[tuple[str, int], int] = {}

        # Sorted list of (chrom, start, end, domain_name) BED intervals
        # for fast overlap checks (stored per chrom for binary search)
        self._domains: dict[str, list] = {}  # chrom → [(start, end, domain_name), ...]

        self._clinvar_loaded = False
        self._domains_loaded = False

        if self._clinvar_tsv and os.path.isfile(self._clinvar_tsv):
            try:
                self._load_clinvar_tsv(self._clinvar_tsv)
                self._clinvar_loaded = True
                logger.info(
                    "[Hotspot] ClinVar TSV loaded: %d hotspot positions (min P/LP=%d)",
                    sum(1 for v in self._plp_counts.values() if v >= self._min_plp),
                    self._min_plp,
                )
            except Exception as exc:
                logger.warning("[Hotspot] Failed to load ClinVar TSV: %s", exc)
        elif self._clinvar_tsv:
            logger.warning("[Hotspot] ClinVar TSV not found: %s", self._clinvar_tsv)

        if self._domains_bed and os.path.isfile(self._domains_bed):
            try:
                self._load_domains_bed(self._domains_bed)
                self._domains_loaded = True
                n_domains = sum(len(v) for v in self._domains.values())
                logger.info("[Hotspot] UniProt domains BED loaded: %d intervals", n_domains)
            except Exception as exc:
                logger.warning("[Hotspot] Failed to load domains BED: %s", exc)
        elif self._domains_bed:
            logger.warning("[Hotspot] Domains BED not found: %s", self._domains_bed)

        if not self._clinvar_loaded and not self._domains_loaded:
            logger.info(
                "[Hotspot] No local data sources loaded — hotspot lookups will "
                "return False (configure hotspot.clinvar_tsv_gz_path for real "
                "hotspot data)"
            )

    # ── ClinVar TSV loading ───────────────────────────────────────────────────

    def _load_clinvar_tsv(self, path: str) -> None:
        """Aggregate P/LP missense counts by (chrom, pos) from ClinVar TSV."""
        open_fn = gzip.open if path.endswith((".gz", ".bgz")) else open

        with open_fn(path, "rt", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                sig = row.get("ClinicalSignificance") or ""
                if not _is_plp(sig):
                    continue

                vtype = row.get("Type") or row.get("VariationClass") or ""
                if not _is_missense_or_small_indel(vtype):
                    continue

                chrom = (
                    (row.get("#Chromosome") or row.get("Chromosome") or "").strip().lstrip("chr")
                )
                if not chrom:
                    continue

                try:
                    pos = int(row.get("PositionVCF") or row.get("Start") or 0)
                except ValueError:
                    continue
                if pos <= 0:
                    continue

                key = (chrom, pos)
                self._plp_counts[key] = self._plp_counts.get(key, 0) + 1

    # ── UniProt domain BED loading ────────────────────────────────────────────

    def _load_domains_bed(self, path: str) -> None:
        """Load a BED file of UniProt functional domains.

        Expected columns (tab-separated, 0-based BED coords):
            chrom  start  end  name  [score  strand ...]
        The name field may be "GENE:domain_name" or just "domain_name".
        """
        open_fn = gzip.open if path.endswith((".gz", ".bgz")) else open

        with open_fn(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or line.startswith("track"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                chrom = parts[0].lstrip("chr")
                try:
                    start = int(parts[1])  # 0-based
                    end = int(parts[2])  # 0-based exclusive
                except ValueError:
                    continue
                domain_name = parts[3].strip() if len(parts) > 3 else "domain"

                self._domains.setdefault(chrom, []).append((start, end, domain_name))

        # Sort each chrom's list by start for binary search
        for chrom in self._domains:
            self._domains[chrom].sort(key=lambda t: t[0])

    # ── Public API ────────────────────────────────────────────────────────────

    def _lookup_internal(self, chrom: str, pos: int) -> HotspotRecord:
        """Return hotspot information for a genomic position (uncached).

        Args:
            chrom: Chromosome (with or without 'chr' prefix).
            pos:   1-based genomic position (VCF convention).

        Returns:
            HotspotRecord with is_hotspot=True if the position qualifies.
            Never raises.
        """
        norm_chrom = chrom.lstrip("chr")

        plp_count = 0
        in_domain = False
        domain_name: str | None = None
        backend = "none"

        # ClinVar P/LP count
        if self._clinvar_loaded:
            plp_count = self._plp_counts.get((norm_chrom, pos), 0)
            backend = "local_clinvar"

        # UniProt domain overlap (BED is 0-based; VCF pos is 1-based)
        if self._domains_loaded:
            pos0 = pos - 1  # convert to 0-based for BED overlap
            intervals = self._domains.get(norm_chrom, [])
            # Binary search for overlapping interval
            lo, hi = 0, len(intervals)
            while lo < hi:
                mid = (lo + hi) // 2
                if intervals[mid][1] <= pos0:
                    lo = mid + 1
                else:
                    hi = mid
            # Check candidates starting from lo going backwards
            for i in range(min(lo, len(intervals) - 1), max(lo - 10, -1), -1):
                s, e, dname = intervals[i]
                if s <= pos0 < e:
                    in_domain = True
                    domain_name = dname
                    break
                if e <= pos0:
                    break
            backend = "local_clinvar+domains" if self._clinvar_loaded else "local_domains"

        is_hotspot = (plp_count >= self._min_plp) or in_domain

        return HotspotRecord(
            chrom=norm_chrom,
            pos=pos,
            plp_count=plp_count,
            in_uniprot_domain=in_domain,
            domain_name=domain_name,
            is_hotspot=is_hotspot,
            backend_used=backend,
        )

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

    def lookup(self, chrom: str, pos: int) -> HotspotRecord:
        """Cached variant position lookup."""
        cache_key = f"{chrom}:{pos}"
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1
        # Call the internal lookup method
        result = self._lookup_internal(chrom, pos)
        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def is_in_hotspot(self, chrom: str, pos: int, gene: str | None = None) -> bool | None:
        """Return True/False if this position's hotspot status is known.

        Returns None (not True/False) when no local data source (ClinVar
        TSV / UniProt domains BED) is loaded — hotspot status was never
        actually assessed for this position, so callers must not treat
        that as a confirmed "not a hotspot" (see PM1 in
        pipeline/acmg/classifier.py, which maps this to STATUS_NOT_EVALUATED
        rather than a false negative).

        Args:
            chrom: Chromosome.
            pos:   1-based position.
            gene:  Gene symbol. Currently unused — retained in the signature
                   because callers pass it positionally; no gene-level
                   fallback source is wired in.
        """
        if self._clinvar_loaded or self._domains_loaded:
            return self.lookup(chrom, pos).is_hotspot
        return None
