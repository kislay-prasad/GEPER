"""
pipeline/gnomad/lookup.py
─────────────────────────
gnomAD allele-frequency lookup.

Supports two backends selected automatically on init:
  LOCAL  — tabix query against a local VCF/BCF file
  API    — gnomAD GraphQL REST endpoint

Usage::

    from pipeline.gnomad.lookup import GnomadLookup

    gn = GnomadLookup(cfg={"gnomad": {"dataset": "gnomad_r3"}})
    hit = gn.lookup("17", 43057051, "A", "T")
    if hit:
        print(hit.af, hit.af_popmax)
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

from pipeline.fastq.errors import FastqPipelineError, _require, _run
from pipeline.gnomad.cache import GnomadDiskCache
from pipeline.utils.http import _api_get  # noqa: F401 — available for GET calls

logger = logging.getLogger("geper.pipeline.gnomad.lookup")

_GNOMAD_GRAPHQL = "https://gnomad.broadinstitute.org/api"

_QUERY_TEMPLATE = """\
{
  variant(variantId: "%(variant_id)s", dataset: %(dataset)s) {
    genome {
      af
      populations {
        id
        ac
        an
      }
    }
  }
}
"""


class GnomadLookupOutcome(Enum):
    """Distinguishes the three states that the caller needs to differentiate.

    ABSENT  — database was reachable and the variant is genuinely not present.
              PM2 *should* be awarded.
    PRESENT — variant was found; GnomadHit carries the frequency data.
    UNAVAILABLE — the database/API could not be reached (network error, tabix
              missing, corrupted file).  PM2 must NOT be awarded.
    """
    ABSENT = "absent"
    PRESENT = "present"
    UNAVAILABLE = "unavailable"


@dataclass
class GnomadHit:
    """Population frequency data for one variant from gnomAD."""
    af: float
    af_popmax: float
    ac: int
    an: int
    backend_used: str
    outcome: GnomadLookupOutcome = GnomadLookupOutcome.PRESENT


class GnomadLookup:
    """Look up allele frequency in gnomAD.

    Args:
        cfg: Full pipeline config dict. Reads ``cfg["gnomad"]``.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        gn_cfg: Dict = (cfg or {}).get("gnomad", {}) or {}
        # FIX (Issue 1): respect `gnomad.enabled: false` in config.
        self._enabled: bool = bool(gn_cfg.get("enabled", True))
        self._vcf_path: Optional[str] = gn_cfg.get("vcf_path") or None
        # FIX (Issue 2): config/default.yaml has always shipped this key as
        # `graphql_dataset`, but this constructor was reading `dataset` —
        # a key mismatch that meant the configured dataset name was never
        # actually applied. `dataset` is kept as a fallback for anyone who
        # set the old (undocumented) key directly.
        self._dataset: str = gn_cfg.get("graphql_dataset") or gn_cfg.get("dataset", "gnomad_r4")
        self._timeout: int = int(gn_cfg.get("timeout", 20))
        self._max_retries: int = int(gn_cfg.get("max_retries", 3))
        self._retry_backoff_secs: float = float(gn_cfg.get("retry_backoff_secs", 1.0))
        self._max_concurrent: int = max(1, int(gn_cfg.get("max_concurrent", 4)))

        # Detect chr prefix requirement for local tabix
        self._use_chr_prefix = False
        if self._vcf_path:
            fn = os.path.basename(self._vcf_path).lower()
            if "grch38" in fn or "hg38" in fn:
                self._use_chr_prefix = True

        # ── Per-run in-memory cache ──
        # Holds PRESENT, ABSENT, *and* UNAVAILABLE — UNAVAILABLE is only
        # ever cached here (never on disk; see cache.py's docstring).
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        # ── Persistent disk cache (PRESENT/ABSENT only) ──
        cache_dir = gn_cfg.get("cache_dir") or None
        cache_path = os.path.join(cache_dir, "gnomad_cache.sqlite3") if cache_dir else None
        self._disk_cache = GnomadDiskCache(
            path=cache_path,
            ttl_hours=gn_cfg.get("cache_ttl_hours"),
        )

        if not self._enabled:
            self._backend = "disabled"
            # FIX (Issue 1): exact, clearly-greppable message required by
            # the offline-mode spec, in addition to the more detailed
            # message below.
            logger.info("gnomAD lookup skipped (disabled)")
            logger.info(
                "[gnomAD] Lookup disabled via config (gnomad.enabled: false) — "
                "skipping local tabix and all network/API requests; "
                "lookups will return UNAVAILABLE."
            )
        elif self._vcf_path and os.path.isfile(self._vcf_path):
            self._backend = "local"
            logger.info("[gnomAD] Backend: local tabix (%s)", self._vcf_path)
        else:
            self._backend = "api"
            logger.info(
                "[gnomAD] Backend: GraphQL API (dataset=%s, max_concurrent=%d, "
                "disk_cache=%s)",
                self._dataset, self._max_concurrent,
                self._disk_cache.path if self._disk_cache.available else "unavailable",
            )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Whether gnomAD lookups are enabled (gnomad.enabled in config)."""
        return self._enabled

    def clear_cache(self) -> None:
        """Reset the in-memory lookup cache. Does NOT clear the
        persistent disk cache — that's a deliberate, separate action
        (see clear_disk_cache) since disk-cached PRESENT/ABSENT entries
        represent real database facts worth keeping across runs."""
        with self._cache_lock:
            self._cache.clear()
            self._cache_hits = 0
            self._cache_misses = 0

    def cache_stats(self) -> dict:
        """Return cache hit/miss statistics (in-memory cache) plus
        persistent disk-cache size."""
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "size": len(self._cache),
            "disk_cache": self._disk_cache.stats(),
        }

    @staticmethod
    def _cache_key(chrom: str, pos: int, ref: str, alt: str) -> str:
        return f"{chrom.lstrip('chr')}:{pos}:{ref.upper()}:{alt.upper()}"

    def _disk_entry_to_result(self, entry: dict) -> Union[GnomadHit, GnomadLookupOutcome]:
        if entry["outcome"] == "absent":
            return GnomadLookupOutcome.ABSENT
        return GnomadHit(
            af=entry["af"] or 0.0,
            af_popmax=entry["af_popmax"] or 0.0,
            ac=entry["ac"] or 0,
            an=entry["an"] or 0,
            backend_used=entry.get("backend") or "disk_cache",
            outcome=GnomadLookupOutcome.PRESENT,
        )

    def _persist_result(self, cache_key: str, result: Union[GnomadHit, GnomadLookupOutcome]) -> None:
        """Write PRESENT/ABSENT to the disk cache. UNAVAILABLE is
        intentionally never persisted — see cache.py's module docstring."""
        if isinstance(result, GnomadHit):
            self._disk_cache.put(
                cache_key, "present",
                af=result.af, af_popmax=result.af_popmax,
                ac=result.ac, an=result.an, backend=result.backend_used,
            )
        elif result == GnomadLookupOutcome.ABSENT:
            self._disk_cache.put(cache_key, "absent")

    def lookup(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
    ) -> Union[GnomadHit, GnomadLookupOutcome]:
        """Look up a variant by coordinates.

        Returns:
            GnomadHit           — variant present; carries frequency data.
            GnomadLookupOutcome.ABSENT      — database reachable; variant not found.
            GnomadLookupOutcome.UNAVAILABLE — database/API unreachable or failed.

        Never raises.
        """
        # FIX (Issue 1): when disabled, never touch network/local backends.
        # Return UNAVAILABLE (not ABSENT) so callers do not mistakenly award
        # PM2 — "unavailable" correctly signals "no usable evidence" rather
        # than "confirmed absent from the population database".
        if not self._enabled:
            logger.debug(
                "[gnomAD] Skipped lookup for %s:%d %s>%s — gnomad.enabled is false",
                chrom, pos, ref, alt,
            )
            return GnomadLookupOutcome.UNAVAILABLE

        cache_key = self._cache_key(chrom, pos, ref, alt)
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1

        # Disk cache (persists across runs/sessions) — checked before any
        # network/tabix call, for both backends.
        disk_entry = self._disk_cache.get(cache_key)
        if disk_entry is not None:
            result = self._disk_entry_to_result(disk_entry)
            with self._cache_lock:
                self._cache[cache_key] = result
            return result

        try:
            if self._backend == "local":
                result = self._tabix_lookup(chrom, pos, ref, alt)
            else:
                result = self._api_lookup(chrom, pos, ref, alt)
        except Exception as exc:
            logger.warning("[gnomAD] lookup failed for %s:%d %s>%s: %s", chrom, pos, ref, alt, exc)
            result = GnomadLookupOutcome.UNAVAILABLE

        with self._cache_lock:
            self._cache[cache_key] = result
        self._persist_result(cache_key, result)
        return result

    def lookup_batch(
        self,
        variants: List[Tuple[str, int, str, str]],
        max_workers: Optional[int] = None,
    ) -> Dict[str, Union[GnomadHit, GnomadLookupOutcome]]:
        """Look up many variants at once, keyed by the same cache-key
        format `lookup()` uses internally (`chrom:pos:REF:ALT`, chrom
        without a 'chr' prefix, ref/alt upper-cased) so callers can look
        results up with `self._cache_key(...)` or just call `lookup()`
        again afterwards and hit a warm cache.

        - Duplicate variants in the input are deduped: each distinct
          variant is only ever looked up once, no matter how many times
          it appears in `variants`.
        - Memory cache and disk cache are checked first; only genuine
          misses reach the network/tabix backend.
        - Remaining misses are dispatched across a bounded thread pool
          (`max_workers`, default `gnomad.max_concurrent` from config)
          — bounded specifically to avoid recreating the 429 storm this
          method exists to fix.
        - Never raises: a failure for one variant becomes UNAVAILABLE
          for that variant only; the rest of the batch is unaffected.

        Returns a dict from cache_key -> result, containing every
        distinct input variant. Also populates the in-memory (and, for
        PRESENT/ABSENT, disk) cache as a side effect, so a subsequent
        `lookup()` call for any of these variants is a cache hit.
        """
        if not self._enabled:
            return {
                self._cache_key(c, p, r, a): GnomadLookupOutcome.UNAVAILABLE
                for c, p, r, a in variants
            }

        workers = max_workers or self._max_concurrent
        results: Dict[str, Union[GnomadHit, GnomadLookupOutcome]] = {}
        to_fetch: Dict[str, Tuple[str, int, str, str]] = {}

        for chrom, pos, ref, alt in variants:
            cache_key = self._cache_key(chrom, pos, ref, alt)
            if cache_key in results or cache_key in to_fetch:
                continue  # duplicate within this batch — only ever fetched once
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                results[cache_key] = cached
                continue
            to_fetch[cache_key] = (chrom, pos, ref, alt)

        if to_fetch:
            disk_hits = self._disk_cache.get_many(to_fetch.keys())
            for cache_key, entry in disk_hits.items():
                result = self._disk_entry_to_result(entry)
                results[cache_key] = result
                with self._cache_lock:
                    self._cache[cache_key] = result
                del to_fetch[cache_key]

        if not to_fetch:
            return results

        logger.info(
            "[gnomAD] Batch prefetch: %d cached, %d to fetch via %s backend "
            "(max_concurrent=%d)",
            len(results), len(to_fetch), self._backend, workers,
        )

        def _fetch_one(cache_key: str, variant: Tuple[str, int, str, str]):
            chrom, pos, ref, alt = variant
            try:
                if self._backend == "local":
                    return cache_key, self._tabix_lookup(chrom, pos, ref, alt)
                return cache_key, self._api_lookup(chrom, pos, ref, alt)
            except Exception as exc:
                logger.warning(
                    "[gnomAD] batch lookup failed for %s:%d %s>%s: %s",
                    chrom, pos, ref, alt, exc,
                )
                return cache_key, GnomadLookupOutcome.UNAVAILABLE

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_one, cache_key, variant): cache_key
                for cache_key, variant in to_fetch.items()
            }
            for future in as_completed(futures):
                cache_key, result = future.result()
                results[cache_key] = result
                with self._cache_lock:
                    self._cache[cache_key] = result
                self._persist_result(cache_key, result)

        return results

    # ── Local backend ─────────────────────────────────────────────────────────

    def _tabix_lookup(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
    ) -> Union[GnomadHit, GnomadLookupOutcome]:
        norm = chrom.lstrip("chr")
        query_chrom = f"chr{norm}" if self._use_chr_prefix else norm

        try:
            tabix_bin = _require("tabix", "gnomad.tabix")
        except FastqPipelineError as exc:
            logger.warning(
                "[gnomAD] tabix not found — cannot perform local lookup for %s:%d %s>%s (%s)",
                chrom, pos, ref, alt, exc,
            )
            return GnomadLookupOutcome.UNAVAILABLE

        try:
            result = _run(
                [tabix_bin, self._vcf_path, f"{query_chrom}:{pos}-{pos}"],
                stage="gnomad.tabix",
            )
        except Exception as exc:
            logger.warning("[gnomAD] tabix run failed for %s:%d: %s", chrom, pos, exc)
            return GnomadLookupOutcome.UNAVAILABLE

        if not result.stdout.strip():
            return GnomadLookupOutcome.ABSENT

        for line in result.stdout.splitlines():
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            vcf_chrom = parts[0].lstrip("chr")
            vcf_pos = int(parts[1])
            vcf_ref = parts[3].upper()
            vcf_alt = parts[4].upper()
            if vcf_chrom != norm or vcf_pos != pos or vcf_ref != ref.upper() or vcf_alt != alt.upper():
                continue

            info = parts[7]
            af = self._parse_info_float(info, "AF")
            af_popmax = self._parse_info_float(info, "AF_popmax")
            ac = int(self._parse_info_float(info, "AC") or 0)
            an = int(self._parse_info_float(info, "AN") or 0)
            if af_popmax is None:
                af_popmax = af or 0.0

            return GnomadHit(
                af=af or 0.0,
                af_popmax=af_popmax,
                ac=ac,
                an=an,
                backend_used="local",
                outcome=GnomadLookupOutcome.PRESENT,
            )
        # tabix returned records for the region but none matched our exact variant
        return GnomadLookupOutcome.ABSENT

    @staticmethod
    def _parse_info_float(info: str, key: str) -> Optional[float]:
        for field in info.split(";"):
            if field.startswith(key + "="):
                try:
                    return float(field.split("=", 1)[1].split(",")[0])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _retry_delay(resp: "requests.Response", fallback_delay: float, max_sleep: float) -> float:
        """Prefer the server's own `Retry-After` header (seconds or an
        HTTP-date) over our own exponential guess — it's the whole point
        of the header, and ignoring it is a common cause of clients
        continuing to hammer a rate limiter right through its own
        requested cooldown. Falls back to exponential backoff + jitter
        (the standard fix for many clients retrying in lockstep and
        re-triggering the same rate limit together) when the header is
        absent or unparseable.
        """
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        if retry_after:
            try:
                return min(float(retry_after), max_sleep)
            except (TypeError, ValueError):
                pass  # not a plain integer — fall through to HTTP-date parsing
            try:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                seconds = (target - datetime.now(timezone.utc)).total_seconds()
                if seconds > 0:
                    return min(seconds, max_sleep)
            except Exception:
                pass
        return min(fallback_delay, max_sleep) + random.uniform(0, fallback_delay * 0.25)

    # ── API backend ───────────────────────────────────────────────────────────

    def _api_lookup(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
    ) -> Union[GnomadHit, GnomadLookupOutcome]:
        norm = chrom.lstrip("chr")
        variant_id = f"{norm}-{pos}-{ref.upper()}-{alt.upper()}"
        query = _QUERY_TEMPLATE % {"variant_id": variant_id, "dataset": self._dataset}

        _RETRY_STATUS = {429, 500, 502, 503, 504}
        delay = self._retry_backoff_secs
        max_retries = self._max_retries
        max_sleep = 30.0  # cap any single sleep (incl. a server-supplied Retry-After)
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
                    sleep_for = self._retry_delay(resp, delay, max_sleep)
                    logger.warning(
                        "[gnomAD API] HTTP %d on attempt %d/%d — retrying in %.1fs",
                        resp.status_code, attempt, max_retries, sleep_for,
                    )
                    time.sleep(sleep_for)
                    delay = min(delay * 2, max_sleep)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except (ReqConnectionError, Timeout) as exc:
                if attempt < max_retries:
                    sleep_for = min(delay, max_sleep) + random.uniform(0, delay * 0.25)
                    logger.warning(
                        "[gnomAD API] %s on attempt %d/%d — retrying in %.1fs: %s",
                        type(exc).__name__, attempt, max_retries, sleep_for, exc,
                    )
                    time.sleep(sleep_for)
                    delay = min(delay * 2, max_sleep)
                else:
                    logger.error("[gnomAD API] %s on final attempt: %s", type(exc).__name__, exc)
                    return GnomadLookupOutcome.UNAVAILABLE
            except Exception as exc:
                logger.warning("[gnomAD API] Request failed for %s: %s", variant_id, exc)
                return GnomadLookupOutcome.UNAVAILABLE
        else:
            logger.error("[gnomAD API] All retries exhausted for %s", variant_id)
            return GnomadLookupOutcome.UNAVAILABLE

        if data is None:
            return GnomadLookupOutcome.UNAVAILABLE

        try:
            genome = data["data"]["variant"]["genome"]
        except (KeyError, TypeError):
            # GraphQL returned but variant key missing — treat as UNAVAILABLE
            return GnomadLookupOutcome.UNAVAILABLE

        if genome is None:
            # variant key present but no genome data → genuinely absent from gnomAD
            return GnomadLookupOutcome.ABSENT

        af = float(genome.get("af") or 0.0)
        populations: List[Dict] = genome.get("populations") or []

        af_popmax = 0.0
        total_ac = 0
        total_an = 0
        for pop in populations:
            ac = int(pop.get("ac") or 0)
            an = int(pop.get("an") or 0)
            total_ac += ac
            total_an += an
            pop_af = ac / an if an > 0 else 0.0
            if pop_af > af_popmax:
                af_popmax = pop_af

        return GnomadHit(
            af=af,
            af_popmax=af_popmax,
            ac=total_ac,
            an=total_an,
            backend_used="api",
            outcome=GnomadLookupOutcome.PRESENT,
        )
