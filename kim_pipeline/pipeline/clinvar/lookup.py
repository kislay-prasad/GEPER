"""
pipeline/clinvar/lookup.py
──────────────────────────
ClinVar variant significance lookup.

Supports two backends:
  LOCAL  — parses a local variant_summary.txt.gz file
  API    — queries NCBI Entrez eutils REST endpoints

Usage::

    from pipeline.clinvar.lookup import ClinVarLookup

    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": "/data/variant_summary.txt.gz"}})
    hit = lookup.lookup("17", 43057051, "A", "T")
    if hit:
        print(hit.significance, hit.review_stars)
    score = ClinVarLookup.sig_to_score("Pathogenic", 3)
"""
from __future__ import annotations

import csv
import gzip
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import requests

from pipeline.utils.http import _api_get

logger = logging.getLogger("geper.pipeline.clinvar.lookup")

# ── Review-status → star mapping ──────────────────────────────────────────────
_STAR_MAP: Dict[str, int] = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, single submitter": 1,
}


def _aa3to1(aa: str) -> str:
    """Convert a 3-letter or 1-letter amino acid code to 1-letter.

    FIX 2: Used for PS1 amino-acid comparison.
    Accepts 1-letter codes unchanged. Returns '' for unknown codes.
    """
    if not aa:
        return ""
    aa = aa.strip().capitalize()
    # Already 1-letter
    if len(aa) == 1 and aa.upper() in "ACDEFGHIKLMNPQRSTVWY*X":
        return aa.upper()
    _TABLE = {
        "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C",
        "Gln": "Q", "Glu": "E", "Gly": "G", "His": "H", "Ile": "I",
        "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F", "Pro": "P",
        "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V",
        "Ter": "*", "Sec": "U", "Pyl": "O",
    }
    return _TABLE.get(aa[:3].capitalize(), "")


def _review_stars(status: str) -> int:
    s = status.lower().strip()
    return _STAR_MAP.get(s, 0)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ClinVarHit:
    """One ClinVar record matching a queried variant."""
    significance: str
    review_stars: int
    submitter_count: int
    allele_id: str
    conflicting: bool
    gene: Optional[str] = None
    hgvs_p: Optional[str] = None  # FIX 2: p. notation for PS1 AA comparison


# ── Main class ────────────────────────────────────────────────────────────────

class ClinVarLookup:
    """Look up ClinVar pathogenicity for a genomic variant.

    Args:
        cfg: Full pipeline config dict.  Reads ``cfg["clinvar"]``.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        cv_cfg: Dict = (cfg or {}).get("clinvar", {}) or {}
        # FIX (Issue 1): respect `clinvar.enabled: false` in config — when
        # disabled, the lookup must never perform local-file or network/API
        # lookups; it should consistently return "not available" (None).
        self._enabled: bool = bool(cv_cfg.get("enabled", True))
        self._tsv_gz_path: Optional[str] = cv_cfg.get("tsv_gz_path") or None
        self._ncbi_api_key: Optional[str] = cv_cfg.get("ncbi_api_key") or os.environ.get("NCBI_API_KEY")
        self._rate_limit_delay: float = 1.0 / (10.0 if self._ncbi_api_key else 3.0)

        # Primary index keyed by (chrom, pos, ref, alt)
        self._by_coord: Dict[Tuple[str, int, str, str], ClinVarHit] = {}
        # Secondary index keyed by (gene_symbol, pos)
        self._by_gene_pos: Dict[Tuple[str, int], ClinVarHit] = {}

        self._backend = "api"
        # ── Per-run in-memory cache ──
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        if not self._enabled:
            # FIX (Issue 1): exact, clearly-greppable message required by
            # the offline-mode spec, in addition to the more detailed
            # message below.
            logger.info("ClinVar lookup skipped (disabled)")
            logger.info(
                "[ClinVar] Lookup disabled via config (clinvar.enabled: false) — "
                "skipping local TSV load and all network/API requests; "
                "lookups will return None."
            )
            self._backend = "disabled"
        elif self._tsv_gz_path and os.path.isfile(self._tsv_gz_path):
            try:
                self._load_tsv_gz(self._tsv_gz_path)
                self._backend = "local"
                logger.info(
                    "[ClinVar] Loaded local TSV: %s (%d entries)",
                    self._tsv_gz_path, len(self._by_coord),
                )
            except Exception as exc:
                logger.warning("[ClinVar] Failed to load local TSV: %s — falling back to API", exc)
                self._backend = "api"

    # ── Local loading ─────────────────────────────────────────────────────────

    def _load_tsv_gz(self, path: str) -> None:
        """Parse variant_summary.txt.gz into in-memory dicts."""
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                chrom = row.get("#Chromosome") or row.get("Chromosome") or ""
                chrom = chrom.lstrip("chr")
                try:
                    pos = int(row.get("Start") or row.get("PositionVCF") or 0)
                except ValueError:
                    continue
                ref = (row.get("ReferenceAllele") or "").strip().upper()
                alt = (row.get("AlternateAllele") or "").strip().upper()
                sig = (row.get("ClinicalSignificance") or "").strip()
                status = (row.get("ReviewStatus") or "").strip()
                try:
                    n_sub = int(row.get("NumberSubmitters") or 0)
                except ValueError:
                    n_sub = 0
                allele_id = (row.get("#AlleleID") or row.get("AlleleID") or "").strip()
                gene = (row.get("GeneSymbol") or "").strip() or None

                # FIX 2: extract p. notation from the Name column for PS1 AA comparison
                name_field = (row.get("Name") or "").strip()
                hgvs_p: Optional[str] = None
                if name_field:
                    import re as _re
                    _m = _re.search(r'\(p\.([^)]+)\)', name_field)
                    if _m:
                        hgvs_p = "p." + _m.group(1)

                stars = _review_stars(status)
                conflicting = "conflicting" in status.lower()

                hit = ClinVarHit(
                    significance=sig,
                    review_stars=stars,
                    submitter_count=n_sub,
                    allele_id=allele_id,
                    conflicting=conflicting,
                    gene=gene,
                    hgvs_p=hgvs_p,
                )

                if chrom and pos and ref and alt:
                    self._by_coord[(chrom, pos, ref, alt)] = hit
                if gene and pos:
                    # Keep the most pathogenic hit per (gene, pos)
                    key = (gene, pos)
                    if key not in self._by_gene_pos:
                        self._by_gene_pos[key] = hit

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Whether ClinVar lookups are enabled (clinvar.enabled in config)."""
        return self._enabled

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

    def lookup(self, chrom: str, pos: int, ref: str, alt: str) -> Optional[ClinVarHit]:
        # FIX (Issue 1): when disabled, never touch network/local backends.
        if not self._enabled:
            logger.debug(
                "[ClinVar] Skipped lookup for %s:%d %s>%s — clinvar.enabled is false",
                chrom, pos, ref, alt,
            )
            return None

        # FIX 14: cache_key and norm_chrom were missing — caused NameError on every call.
        norm_chrom = chrom.lstrip("chr")
        ref_upper = ref.upper()
        alt_upper = alt.upper()
        cache_key = (norm_chrom, pos, ref_upper, alt_upper)

        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1

        if self._backend == "local":
            result = self._by_coord.get((norm_chrom, pos, ref_upper, alt_upper))
        else:
            result = self._api_lookup(norm_chrom, pos, ref_upper, alt_upper)

        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def check_same_codon_pathogenic(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        wildtype_aa: Optional[str] = None,
        mutant_aa: Optional[str] = None,
    ) -> tuple:
        """Check for PS1 / PM5 evidence at the same codon.

        Searches ClinVar for Pathogenic/Likely Pathogenic missense variants
        within the codon window (pos-2 to pos+2).

        Returns:
            (same_aa_pathogenic, novel_aa_at_known_pathogenic_codon) tuple
            of Optional[bool].  Both None when insufficient data.

        PS1: a *different* allele at the *same position* (or codon) that
             produces the *same amino-acid change* is already pathogenic.
        PM5: a *different* amino-acid change at the *same codon* is already
             pathogenic.
        """
        if self._backend != "local" or not self._by_coord:
            # API fallback does not support codon-window scans efficiently,
            # and a disabled backend (self._backend == "disabled") must
            # never scan or query — return "insufficient data".
            return (None, None)

        norm_chrom = chrom.lstrip("chr")
        ref_upper = ref.upper()
        alt_upper = alt.upper()

        # FIX 14: check cache before performing O(N) codon-window scan
        _codon_cache_key = ("codon", norm_chrom, pos, ref_upper, alt_upper)
        with self._cache_lock:
            if _codon_cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[_codon_cache_key]
            self._cache_misses += 1

        # Scan codon window (±2 bp covers any codon the SNV could be in)
        _PLP = {"pathogenic", "likely pathogenic", "pathogenic/likely pathogenic"}

        same_aa_plp = False
        novel_aa_plp = False

        for scan_pos in range(pos - 2, pos + 3):
            for (c, p, r, a), hit in self._by_coord.items():
                if c != norm_chrom or p != scan_pos:
                    continue
                if a == alt_upper and r == ref_upper and p == pos:
                    continue  # same variant, skip
                sig = hit.significance.lower().strip()
                if sig not in _PLP:
                    continue
                # Confirmed P/LP variant at codon window — different allele

                # FIX 2: PS1 — same amino-acid substitution in ClinVar.
                if wildtype_aa and mutant_aa and hit.hgvs_p:
                    import re as _re2
                    _p_m = _re2.match(
                        r'p\.([A-Za-z]{1,3})(\d+)([A-Za-z]{1,3})',
                        hit.hgvs_p,
                    )
                    if _p_m:
                        cv_ref_aa = _aa3to1(_p_m.group(1))
                        cv_alt_aa = _aa3to1(_p_m.group(3))
                        our_ref_aa = _aa3to1(wildtype_aa)
                        our_alt_aa = _aa3to1(mutant_aa)
                        if (
                            cv_ref_aa and cv_alt_aa and our_ref_aa and our_alt_aa
                            and cv_ref_aa == our_ref_aa
                            and cv_alt_aa == our_alt_aa
                        ):
                            same_aa_plp = True

                # PM5 — different allele at same codon
                if wildtype_aa and mutant_aa:
                    novel_aa_plp = True

        result = (
            True if same_aa_plp else None,   # PS1
            True if novel_aa_plp else None,  # PM5
        )
        # FIX 14: cache the codon-scan result to avoid repeated O(N) scans
        with self._cache_lock:
            self._cache[("codon", norm_chrom, pos, ref_upper, alt_upper)] = result
        return result

    # ── API backend ───────────────────────────────────────────────────────────

    def _api_lookup(self, chrom: str, pos: int, ref: str, alt: str) -> Optional[ClinVarHit]:
        """Query NCBI Entrez eutils for a variant."""
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        term = f"{chrom}[Chromosome] AND {pos}[Base Position]"
        params: Dict = {
            "db": "clinvar",
            "term": term,
            "retmax": "20",
            "retmode": "json",
        }
        if self._ncbi_api_key:
            params["api_key"] = self._ncbi_api_key

        try:
            time.sleep(self._rate_limit_delay)
            resp = _api_get(base + "esearch.fcgi", params=params, timeout=15, logger=logger)
            if resp is None:
                logger.warning("[ClinVar API] esearch returned None for %s:%d", chrom, pos)
                return None
            resp.raise_for_status()
            ids = resp.json().get("esearchresult", {}).get("idlist", [])
        except Exception as exc:
            logger.warning("[ClinVar API] esearch failed for %s:%d: %s", chrom, pos, exc)
            return None

        if not ids:
            return None

        try:
            time.sleep(self._rate_limit_delay)
            sum_params: Dict = {
                "db": "clinvar",
                "id": ",".join(ids[:5]),
                "retmode": "json",
            }
            if self._ncbi_api_key:
                sum_params["api_key"] = self._ncbi_api_key
            resp2 = _api_get(base + "esummary.fcgi", params=sum_params, timeout=15, logger=logger)
            if resp2 is None:
                logger.warning("[ClinVar API] esummary returned None for ids=%s", ids)
                return None
            resp2.raise_for_status()
            result_set = resp2.json().get("result", {})
        except Exception as exc:
            logger.warning("[ClinVar API] esummary failed for ids=%s: %s", ids, exc)
            return None

        for uid in ids:
            entry = result_set.get(str(uid), {})
            sig = entry.get("clinical_significance", {})
            sig_str = sig.get("description", "") if isinstance(sig, dict) else str(sig)
            review = entry.get("review_status", "")
            stars = _review_stars(review)
            conflicting = "conflicting" in review.lower()
            gene_list = entry.get("genes", [])
            gene = gene_list[0].get("symbol") if gene_list else None
            n_sub = entry.get("supporting_submissions", {}).get("total", 0)
            allele_id = str(entry.get("accession", uid))

            return ClinVarHit(
                significance=sig_str,
                review_stars=stars,
                submitter_count=int(n_sub) if n_sub else 0,
                allele_id=allele_id,
                conflicting=conflicting,
                gene=gene,
            )

        return None

    # ── Score helper ──────────────────────────────────────────────────────────

    @staticmethod
    def sig_to_score(significance: str, stars: int) -> Optional[float]:
        """Convert a ClinVar significance string and star rating to [0, 1].

        Star=0 dampens by 10% toward 0.5 (increases uncertainty).
        Case-insensitive matching.

        Conflicting interpretations are treated as uncertain (0.5) regardless
        of whether the string contains 'pathogenic' — this prevents the
        substring match for 'pathogenic' inside 'conflicting interpretations
        of pathogenicity' from incorrectly awarding a full pathogenic score.

        Returns None for completely unrecognised strings ("Mixed significance",
        "Unknown significance", etc.) — the caller should treat None as
        "no usable ClinVar evidence" rather than "uncertain".

        Args:
            significance: ClinVar clinical significance string.
            stars:        Review star count (0–4).

        Returns:
            Float in [0, 1] or None when the string is unrecognised.
        """
        sig = significance.lower().strip()
        # Check for conflicting first — must precede pathogenic substring match
        if "conflicting" in sig:
            return 0.5  # treat as uncertain regardless of content
        if "pathogenic" in sig and "likely" not in sig:
            base = 1.0
        elif "likely pathogenic" in sig:
            base = 0.75
        elif "uncertain" in sig or "vus" in sig or sig == "uncertain significance":
            base = 0.5
        elif "likely benign" in sig:
            base = 0.25
        elif "benign" in sig:
            base = 0.0
        else:
            # Unrecognised significance string (e.g. "Mixed significance",
            # "Unknown significance", "not provided") — return None so callers
            # know there is no usable evidence rather than mapping to 0.5.
            return None

        if stars == 0 and base != 0.5:
            base = 0.5 + (base - 0.5) * 0.9

        return round(base, 4)
