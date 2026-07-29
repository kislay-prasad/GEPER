"""
pipeline/constraint/lookup.py
──────────────────────────────
gnomAD gene constraint (pLI / LOEUF) lookup.

Replaces the OMIM phenotype-text heuristic for PVS1 LoF-intolerance.

Two backends selected automatically:
  LOCAL  — parses gnomAD constraint TSV (gnomad.v4.1.constraint_metrics.tsv
            or any TSV with columns: gene, pLI, oe_lof_upper).
  API    — gnomAD GraphQL endpoint (same host as GnomadLookup).
  FALLBACK — OmimLookup.is_lof_intolerant() when neither source is available.

Decision rule (ClinGen recommendation):
  lof_gene_intolerant = pLI ≥ 0.9  OR  LOEUF ≤ 0.35

Config::

    gnomad_constraint:
      tsv_path: /data/gnomad.v4.1.constraint_metrics.tsv  # plain or .gz
      pli_threshold: 0.9        # optional, default 0.9
      loeuf_threshold: 0.35     # optional, default 0.35
      timeout: 20               # API timeout seconds

Download the TSV (23 MB gzipped)::

    wget https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/constraint/
         gnomad.v4.1.constraint_metrics.tsv.bgz
    # then set gnomad_constraint.tsv_path to that path
"""
from __future__ import annotations

import gzip
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("geper.pipeline.constraint.lookup")

_GNOMAD_GRAPHQL = "https://gnomad.broadinstitute.org/api"

# GraphQL query for gene-level constraint (gnomAD v4 schema)
_CONSTRAINT_QUERY = """\
{
  gene(gene_symbol: "%(symbol)s", reference_genome: GRCh38) {
    gnomad_constraint {
      pli
      oe_lof_upper
    }
  }
}
"""


@dataclass
class ConstraintRecord:
    """gnomAD constraint metrics for one gene."""
    gene: str
    pli: Optional[float]          # probability of LoF intolerance (0–1)
    loeuf: Optional[float]        # oe_lof_upper (lower = more constrained)
    oe_mis: Optional[float] = None   # FIX 15: observed/expected missense ratio
    mis_z: Optional[float] = None    # FIX 15: missense Z-score
    transcript: Optional[str] = None
    backend_used: str = "unknown"

    def is_lof_intolerant(self, pli_threshold: float = 0.9, loeuf_threshold: float = 0.35) -> bool:
        """Return True if pLI ≥ threshold OR LOEUF ≤ threshold."""
        if self.pli is not None and self.pli >= pli_threshold:
            return True
        if self.loeuf is not None and self.loeuf <= loeuf_threshold:
            return True
        return False

    def is_missense_constrained(
        self,
        oe_mis_threshold: float = 0.8,
        mis_z_threshold: float = 3.09,
    ) -> bool:
        """FIX 15: Return True if gene has low rate of benign missense variation.

        Criteria (ClinGen PP2 guidance):
          oe_mis < oe_mis_threshold (default 0.8) — observed/expected missense < threshold, OR
          mis_z > mis_z_threshold (default 3.09) — high Z-score for missense depletion.
        """
        if self.oe_mis is not None and self.oe_mis < oe_mis_threshold:
            return True
        if self.mis_z is not None and self.mis_z > mis_z_threshold:
            return True
        return False


class GnomadConstraintLookup:
    """Look up gnomAD gene constraint scores (pLI / LOEUF).

    Args:
        cfg: Full pipeline config dict.  Reads ``cfg["gnomad_constraint"]``.
        omim_fallback: Optional OmimLookup instance used when neither local
                       TSV nor API is available.
    """

    def __init__(
        self,
        cfg: Optional[Dict] = None,
        omim_fallback=None,
    ) -> None:
        gc_cfg: Dict = (cfg or {}).get("gnomad_constraint", {}) or {}
        # FIX (Issue 1): the API backend below queries the same gnomAD
        # GraphQL service as pipeline/gnomad/lookup.py. `gnomad.enabled:
        # false` must disable ALL gnomAD API traffic, not just the
        # allele-frequency lookup — otherwise "offline mode" still makes
        # live HTTP requests for gene constraint (PVS1/PP2) data.
        gn_cfg: Dict = (cfg or {}).get("gnomad", {}) or {}
        self._gnomad_enabled: bool = bool(gn_cfg.get("enabled", True))
        self._tsv_path: Optional[str] = gc_cfg.get("tsv_path") or None
        self._pli_threshold: float = float(gc_cfg.get("pli_threshold", 0.9))
        self._loeuf_threshold: float = float(gc_cfg.get("loeuf_threshold", 0.35))
        self._timeout: int = int(gc_cfg.get("timeout", 20))
        self._omim_fallback = omim_fallback
        # ── Per-run in-memory cache ──
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        # gene symbol (uppercase) → ConstraintRecord
        self._by_gene: Dict[str, ConstraintRecord] = {}
        self._backend = "api"

        if self._tsv_path and os.path.isfile(self._tsv_path):
            try:
                self._load_tsv(self._tsv_path)
                self._backend = "local"
                logger.info(
                    "[Constraint] Loaded local TSV: %s (%d genes)",
                    self._tsv_path, len(self._by_gene),
                )
            except Exception as exc:
                logger.warning(
                    "[Constraint] Failed to load TSV %s: %s — falling back to API/OMIM",
                    self._tsv_path, exc,
                )
        elif not self._gnomad_enabled:
            # FIX (Issue 1): no local TSV and gnomAD API disabled — go
            # straight to disabled/OMIM-fallback, never touch the network.
            self._backend = "disabled"
            logger.info(
                "[Constraint] gnomAD API disabled via config (gnomad.enabled: "
                "false) — skipping constraint API requests; falling back to "
                "OMIM heuristic (or unavailable) for LoF-intolerance/constraint."
            )
        else:
            if self._tsv_path:
                logger.warning(
                    "[Constraint] TSV not found: %s — falling back to API/OMIM",
                    self._tsv_path,
                )
            logger.info("[Constraint] Backend: gnomAD GraphQL API (or OMIM fallback)")

    # ── TSV loading ───────────────────────────────────────────────────────────

    def _load_tsv(self, path: str) -> None:
        """Parse gnomAD constraint TSV into self._by_gene.

        Supports both the v2.1 and v4.x column layouts:
          v4.x: gene, transcript, pLI, oe_lof_upper
          v2.1: gene, transcript, pLI, oe_lof_upper (same names, different file)
        Column names are matched case-insensitively.
        """
        open_fn = gzip.open if path.endswith((".gz", ".bgz")) else open

        with open_fn(path, "rt", encoding="utf-8", errors="replace") as fh:
            header_line = None
            for raw in fh:
                line = raw.rstrip("\n")
                if line.startswith("#"):
                    continue
                if header_line is None:
                    header_line = [c.strip().lower() for c in line.split("\t")]
                    continue
                parts = line.split("\t")
                row = dict(zip(header_line, parts))

                gene = (row.get("gene") or "").strip().upper()
                if not gene:
                    continue

                transcript = (row.get("transcript") or "").strip() or None

                pli: Optional[float] = None
                for col in ("pli", "p_li", "p(li)"):
                    raw_val = row.get(col, "").strip()
                    if raw_val and raw_val not in (".", "NA", "nan", ""):
                        try:
                            pli = float(raw_val)
                            break
                        except ValueError:
                            pass

                loeuf: Optional[float] = None
                for col in ("oe_lof_upper", "loeuf", "oe_lof_upper_bin"):
                    raw_val = row.get(col, "").strip()
                    if raw_val and raw_val not in (".", "NA", "nan", ""):
                        try:
                            loeuf = float(raw_val)
                            break
                        except ValueError:
                            pass

                # Keep highest-pLI transcript per gene (canonical choice)
                # FIX 15: also parse missense constraint columns
                oe_mis: Optional[float] = None
                for col in ("oe_mis", "oe_mis_upper"):
                    raw_val = row.get(col, "").strip()
                    if raw_val and raw_val not in (".", "NA", "nan", ""):
                        try:
                            oe_mis = float(raw_val)
                            break
                        except ValueError:
                            pass

                mis_z: Optional[float] = None
                for col in ("mis_z", "z_mis", "mis_z_score"):
                    raw_val = row.get(col, "").strip()
                    if raw_val and raw_val not in (".", "NA", "nan", ""):
                        try:
                            mis_z = float(raw_val)
                            break
                        except ValueError:
                            pass

                existing = self._by_gene.get(gene)
                if existing is None or (pli is not None and (existing.pli or 0.0) < pli):
                    self._by_gene[gene] = ConstraintRecord(
                        gene=gene,
                        pli=pli,
                        loeuf=loeuf,
                        oe_mis=oe_mis,
                        mis_z=mis_z,
                        transcript=transcript,
                        backend_used="local",
                    )

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

    def lookup(self, gene_symbol: str) -> Optional[ConstraintRecord]:
        """Return constraint metrics for a gene symbol. Results are cached per-run."""
        cache_key = (gene_symbol or "").lower()
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1
        result = self._uncached_lookup(gene_symbol)
        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def _uncached_lookup(self, gene_symbol: str) -> Optional[ConstraintRecord]:
        """Internal lookup without cache layer.

        Returns None if the gene is not found in any backend.
        Never raises — failures are logged and return None.
        """
        if not gene_symbol:
            return None
        upper = gene_symbol.upper()

        if self._backend == "local":
            return self._by_gene.get(upper)

        if self._backend == "disabled":
            # FIX (Issue 1): never call the API when gnomAD is disabled.
            return None

        # API
        try:
            return self._api_lookup(upper)
        except Exception as exc:
            logger.warning("[Constraint] API lookup failed for %s: %s", gene_symbol, exc)
            return None

    def is_lof_intolerant(self, gene_symbol: str) -> bool:
        """Return True if this gene is LoF intolerant per gnomAD constraint.

        Decision rule: pLI ≥ pli_threshold OR LOEUF ≤ loeuf_threshold.

        Falls back to OmimLookup.is_lof_intolerant() if constraint data is
        unavailable (no local file, API unreachable, gene not found).
        """
        rec = self.lookup(gene_symbol)
        if rec is not None:
            return rec.is_lof_intolerant(self._pli_threshold, self._loeuf_threshold)

        # Fallback: OMIM heuristic
        if self._omim_fallback is not None:
            try:
                result = self._omim_fallback.is_lof_intolerant(gene_symbol)
                logger.debug(
                    "[Constraint] %s not found in gnomAD constraint — OMIM fallback: %s",
                    gene_symbol, result,
                )
                return result
            except Exception as exc:
                logger.debug("[Constraint] OMIM fallback failed for %s: %s", gene_symbol, exc)

        return False

    def is_missense_constrained(self, gene_symbol: str) -> bool:
        """FIX 15: Return True if gene has low rate of benign missense variation.

        Used for PP2 (ACMG/ClinGen): missense constraint requires oe_mis < 0.8
        or mis_z > 3.09. This is distinct from LoF intolerance (pLI / LOEUF).
        Falls back to False when constraint data is unavailable.
        """
        rec = self.lookup(gene_symbol)
        if rec is not None:
            return rec.is_missense_constrained()
        # No fallback for missense constraint — OMIM does not capture this metric
        return False

    # ── API backend ───────────────────────────────────────────────────────────

    def _api_lookup(self, gene_symbol: str) -> Optional[ConstraintRecord]:
        """Query gnomAD GraphQL for gene constraint scores."""
        try:
            import requests
            from requests.exceptions import ConnectionError as ReqConnectionError, Timeout
        except ImportError:
            logger.warning("[Constraint] requests not installed — cannot use API backend")
            return None

        import time as _time

        query = _CONSTRAINT_QUERY % {"symbol": gene_symbol}
        _RETRY_STATUS = {429, 500, 502, 503, 504}
        delay = 1.0
        max_retries = 3
        data = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    _GNOMAD_GRAPHQL,
                    json={"query": query},
                    timeout=self._timeout,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code in _RETRY_STATUS and attempt < max_retries:
                    logger.warning(
                        "[Constraint API] HTTP %d on attempt %d/%d — retrying in %.0fs",
                        resp.status_code, attempt, max_retries, delay,
                    )
                    _time.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except (ReqConnectionError, Timeout) as exc:
                if attempt < max_retries:
                    logger.warning(
                        "[Constraint API] %s on attempt %d/%d — retrying in %.0fs: %s",
                        type(exc).__name__, attempt, max_retries, delay, exc,
                    )
                    _time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("[Constraint API] %s on final attempt: %s", type(exc).__name__, exc)
                    return None
            except Exception as exc:
                logger.warning("[Constraint API] Request failed for %s: %s", gene_symbol, exc)
                return None
        else:
            logger.error("[Constraint API] All retries exhausted for %s", gene_symbol)
            return None

        if data is None:
            return None

        try:
            constraint = data["data"]["gene"]["gnomad_constraint"]
        except (KeyError, TypeError):
            return None

        if constraint is None:
            return None

        pli_raw = constraint.get("pli")
        loeuf_raw = constraint.get("oe_lof_upper")

        pli = float(pli_raw) if pli_raw is not None else None
        loeuf = float(loeuf_raw) if loeuf_raw is not None else None

        return ConstraintRecord(
            gene=gene_symbol,
            pli=pli,
            loeuf=loeuf,
            backend_used="api",
        )
