"""
pipeline/clingen/lookup.py
───────────────────────────
ClinGen Dosage Sensitivity lookup — LoF-intolerance fallback for
pipeline/constraint/lookup.py::GnomadConstraintLookup.is_lof_intolerant(),
used when a gene has no gnomAD pLI/LOEUF record (no local TSV coverage,
or the gnomAD API is disabled/unreachable). Replaces the role the now-
retired OMIM phenotype-text heuristic used to serve, with a real curated
score instead of a regex over free-text phenotype descriptions.

Deliberately NOT a reuse of geper/pipeline/clingen/*: that module is
gene-*resolution*-capable (Ensembl overlap + PVS1 transcript + MANE
disambiguation, to map a variant to a gene symbol) and threaded through
geper-specific globals (`from config import CONFIG`, `from utils.logger
import get_logger`, `from utils.service_health import HEALTH`). kim_pipeline
already resolves gene_name via VEP annotation upstream (see
pipeline/orchestration/shared.py) and only ever needs a gene-symbol ->
dosage-sensitivity-score lookup, so pulling in geper's full client would
mean depending on a sibling, separately-packaged project (its own
pyproject.toml, name "geper") whose top-level `pipeline` and `config`
package names collide directly with kim_pipeline's own `pipeline` package
if both were ever placed on sys.path together — not a clean shared import.
This module ports only the data-format knowledge (ClinGen's published
column names and its 0/1/2/3/30/40 dosage-sensitivity scale — public
information from ClinGen's own download docs, not application code) into
kim_pipeline's own cfg-dict-driven, local-file-then-API pattern (see
pipeline/omim/lookup.py's now-removed twin, and pipeline/constraint/lookup.py
for the pattern this mirrors).

Supports two backends:
  LOCAL  — parses a local ClinGen Dosage Sensitivity flat-file download
           (https://search.clinicalgenome.org/kb/dosage/download, or the
           ftp.clinicalgenome.org mirror). Primary source: this is a
           periodically-refreshed curated dataset, not a per-request
           service, so a local copy is both faster and more reliable.
  API    — HTTP fallback for genes not present in the local dataset.
           NOT LIVE-VERIFIED in this environment (no network route to
           clinicalgenome.org here — see geper/pipeline/clingen/provider.py's
           docstring for the same caveat on the sibling project's own,
           independently-written API client). Disabled by default in
           config/default.yaml until spot-checked against a real response;
           the local-dataset path is the only verified path today.

License: ClinGen's curated content (gene-disease validity, dosage
sensitivity, ERepo) is published under CC0 1.0 Universal — public domain,
no restriction on commercial use, attribution requested but not required
(https://clinicalgenome.org/docs/terms-of-use/, verified in
geper/DATA_SOURCE_LICENSE_AUDIT.md). CC0 is a blanket public-domain
dedication with no field-of-use or per-deployment restriction, so that
verification covers kim_pipeline's separate commercial deployment the
same way it covers geper/'s — this is not an assumption carried over
unchecked, the license terms themselves are unconditional.

Usage::

    from pipeline.clingen.lookup import ClinGenDosageLookup

    clingen = ClinGenDosageLookup(cfg={"clingen": {"dosage_sensitivity_path": "/data/clingen_dosage.tsv"}})
    rec = clingen.lookup_gene("BRCA1")
    print(rec.haploinsufficiency_score)
    print(clingen.is_dosage_sufficient_for_lof("BRCA1"))
"""

from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional

from pipeline.utils.http import _api_get

logger = logging.getLogger("geper.pipeline.clingen.lookup")

# ClinGen's own Dosage Sensitivity curation scale (Score column of its
# published download) — GEPER never invents its own scale, only reads
# ClinGen's. See https://search.clinicalgenome.org/kb/dosage for the SOP.
DOSAGE_SCORE_LABELS: Dict[int, str] = {
    0: "No evidence available",
    1: "Little evidence for dosage pathogenicity",
    2: "Some evidence for dosage pathogenicity",
    3: "Sufficient evidence for dosage pathogenicity",
    30: "Gene associated with autosomal recessive phenotype",
    40: "Dosage sensitivity unlikely",
}

# Scores that establish haploinsufficiency — i.e. that losing ONE allele
# is sufficient to cause disease. An explicit SET, not a threshold,
# because ClinGen's Score column is NOT one ordinal range: 0-3 are
# evidence levels, and 30/40 are special codes (see DOSAGE_SCORE_LABELS
# above). Any `>=` comparison silently admits both special codes, which
# is the defect this replaced — `40 >= 3` is True, so a gene ClinGen had
# affirmatively curated as "dosage sensitivity unlikely" was reported as
# LoF-intolerant.
#
# Deliberately a whitelist of what qualifies, NOT a blacklist of {30, 40}.
# A blacklist passes every test we could write today and silently breaks
# the first time ClinGen adds a code — it preserves the exact shape of
# the bug while hiding its current instance.
#
# 3 is the only qualifying score. 0/1/2 are "no"/"little"/"some"
# evidence; 30 says the gene's disease model is autosomal recessive, so
# one damaged allele is NOT sufficient; 40 says dosage sensitivity is
# unlikely outright. Both special codes argue AGAINST haploinsufficiency,
# which is why reading them as support was a sign error rather than
# merely an out-of-range value.
DOSAGE_SUFFICIENT_EVIDENCE_SCORES: FrozenSet[int] = frozenset({3})

# ClinGen's real download carries a preamble before the header row (a
# title/date/URL block for the KB export, '#'-prefixed title/date/note
# lines for the ftp mirror — the ftp mirror's last '#' line *is* the real
# header). A bare csv.DictReader over line 1 silently misparses both
# shapes; scan for the header by content instead. Both real column-name
# casings ClinGen publishes are listed so either download variant parses.
_GENE_SYMBOL_COLUMNS = ("GENE SYMBOL", "Gene Symbol", "gene_symbol")
_HAPLOINSUFFICIENCY_SCORE_COLUMNS = (
    "HAPLOINSUFFICIENCY SCORE",
    "Haploinsufficiency Score",
    "haploinsufficiency_score",
)
_HAPLOINSUFFICIENCY_DESC_COLUMNS = (
    "HAPLOINSUFFICIENCY DESCRIPTION",
    "Haploinsufficiency Description",
    "haploinsufficiency_description",
)
_TRIPLOSENSITIVITY_SCORE_COLUMNS = (
    "TRIPLOSENSITIVITY SCORE",
    "Triplosensitivity Score",
    "triplosensitivity_score",
)
_TRIPLOSENSITIVITY_DESC_COLUMNS = (
    "TRIPLOSENSITIVITY DESCRIPTION",
    "Triplosensitivity Description",
    "triplosensitivity_description",
)


@dataclass
class DosageSensitivityRecord:
    """One ClinGen Dosage Sensitivity curation for one gene."""

    gene: str
    haploinsufficiency_score: Optional[int]
    haploinsufficiency_description: Optional[str] = None
    triplosensitivity_score: Optional[int] = None
    triplosensitivity_description: Optional[str] = None
    backend_used: str = "unknown"

    def is_lof_sufficient(self) -> bool:
        """True only if ClinGen curated this gene's haploinsufficiency score as one of
        DOSAGE_SUFFICIENT_EVIDENCE_SCORES (currently {3}).

        Membership, not a threshold: ClinGen's scale is not ordinal above
        3, so `>=` admitted the special codes 30 and 40 as if they were
        stronger evidence than 3. Both in fact argue against
        haploinsufficiency. `None` (gene not curated, or no data) stays
        False — absent, never assumed.
        """
        return self.haploinsufficiency_score in DOSAGE_SUFFICIENT_EVIDENCE_SCORES


class ClinGenDosageLookup:
    """Look up ClinGen Dosage Sensitivity (haploinsufficiency) scores by gene symbol.

    Args:
        cfg: Full pipeline config dict. Reads ``cfg["clingen"]``.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        cg_cfg: Dict = (cfg or {}).get("clingen", {}) or {}
        self._enabled: bool = bool(cg_cfg.get("enabled", True))
        self._local_path: Optional[str] = cg_cfg.get("dosage_sensitivity_path") or None
        self._api_enabled: bool = bool(cg_cfg.get("api_enabled", False))
        self._api_endpoint: str = (
            cg_cfg.get("api_endpoint") or "https://search.clinicalgenome.org/kb/gene-dosage"
        )
        self._timeout: int = int(cg_cfg.get("timeout", 15))
        self._max_retries: int = int(cg_cfg.get("max_retries", 3))

        self._by_gene: Dict[str, DosageSensitivityRecord] = {}
        self._backend = "disabled"
        # ── Per-run in-memory cache ──
        self._cache: dict = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        if not self._enabled:
            self._backend = "disabled"
            logger.info(
                "[ClinGen] Dosage-sensitivity lookup disabled via config (clingen.enabled: false)."
            )
            return

        import os

        if self._local_path and os.path.isfile(self._local_path):
            try:
                self._load_local(self._local_path)
                self._backend = "local"
                logger.info(
                    "[ClinGen] Loaded local dosage-sensitivity dataset: %s (%d genes)",
                    self._local_path,
                    len(self._by_gene),
                )
            except Exception as exc:
                logger.warning(
                    "[ClinGen] Failed to load dosage-sensitivity file %s: %s", self._local_path, exc
                )
                self._backend = "api" if self._api_enabled else "disabled"
        elif self._api_enabled:
            self._backend = "api"
            logger.info(
                "[ClinGen] Backend: live API (UNVERIFIED against a live response in this deployment)"
            )
        else:
            if self._local_path:
                logger.warning(
                    "[ClinGen] Dosage-sensitivity file not found: %s — lookups will be unavailable",
                    self._local_path,
                )
            self._backend = "disabled"

    # ── Local loading ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_header_cell(cell: str) -> str:
        return cell.strip().lstrip("#").strip()

    @staticmethod
    def _sniff_delimiter(path: str) -> str:
        return "\t" if path.lower().endswith((".tsv", ".txt")) else ","

    @classmethod
    def _pick(cls, row: Dict[str, str], candidates: tuple) -> Optional[str]:
        for name in candidates:
            if name in row and row[name] not in (None, ""):
                return row[name]
        return None

    @staticmethod
    def _to_int(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _load_local(self, path: str) -> None:
        """Parse ClinGen's Dosage Sensitivity TSV/CSV download into self._by_gene.

        Tolerates both real ClinGen export shapes: the interactive KB
        export (title/date/URL preamble, ALLCAPS header) and the ftp
        mirror (#-prefixed preamble, the last # line being the real
        title-case header) — see this module's docstring.
        """
        delimiter = self._sniff_delimiter(path)
        with open(path, "r", encoding="utf-8", newline="", errors="replace") as fh:
            lines = fh.readlines()

        header_idx = None
        header_cells: List[str] = []
        for idx, line in enumerate(lines):
            cells = next(csv.reader([line], delimiter=delimiter), [])
            if cells and self._normalize_header_cell(cells[0]).lower() == "gene symbol":
                header_idx = idx
                header_cells = [
                    self._normalize_header_cell(c) if i == 0 else c.strip()
                    for i, c in enumerate(cells)
                ]
                break
        if header_idx is None:
            logger.warning(
                "[ClinGen] No recognizable 'Gene Symbol' header found in %s — treating as empty",
                path,
            )
            return

        count = 0
        for line in lines[header_idx + 1 :]:
            cells = next(csv.reader([line], delimiter=delimiter), [])
            if not cells or not cells[0].strip():
                continue
            if all(set(c.strip()) <= {"+"} for c in cells if c.strip()):
                continue  # KB export's "+++"-only separator row
            row = dict(zip(header_cells, cells))

            gene = (self._pick(row, _GENE_SYMBOL_COLUMNS) or "").strip().upper()
            if not gene:
                continue

            record = DosageSensitivityRecord(
                gene=gene,
                haploinsufficiency_score=self._to_int(
                    self._pick(row, _HAPLOINSUFFICIENCY_SCORE_COLUMNS)
                ),
                haploinsufficiency_description=self._pick(row, _HAPLOINSUFFICIENCY_DESC_COLUMNS),
                triplosensitivity_score=self._to_int(
                    self._pick(row, _TRIPLOSENSITIVITY_SCORE_COLUMNS)
                ),
                triplosensitivity_description=self._pick(row, _TRIPLOSENSITIVITY_DESC_COLUMNS),
                backend_used="local",
            )
            # ClinGen's download carries one row per gene; a duplicate
            # (re-curation) keeps the last row, matching the file's own
            # "most recent row wins" convention.
            self._by_gene[gene] = record
            count += 1

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

    def lookup_gene(self, gene_symbol: str) -> Optional[DosageSensitivityRecord]:
        """Return the Dosage Sensitivity record for a gene symbol (case-insensitive). Results cached per-run."""
        if not gene_symbol:
            return None
        cache_key = gene_symbol.upper()
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
            self._cache_misses += 1

        if self._backend == "local":
            result = self._by_gene.get(cache_key)
        elif self._backend == "api":
            result = self._api_lookup_gene(cache_key)
        else:
            result = None

        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def is_dosage_sufficient_for_lof(self, gene_symbol: str) -> bool:
        """Return True if ClinGen curated this gene's haploinsufficiency score as sufficient
        evidence for LoF (membership of DOSAGE_SUFFICIENT_EVIDENCE_SCORES, currently {3}).

        Returns False if no dosage-sensitivity data is available for this
        gene (no local dataset, API unreachable/disabled, or gene not
        curated) — never guesses.
        """
        record = self.lookup_gene(gene_symbol)
        if record is None:
            return False
        return record.is_lof_sufficient()

    # ── API backend (UNVERIFIED — see module docstring) ─────────────────────────

    def _api_lookup_gene(self, gene_symbol: str) -> Optional[DosageSensitivityRecord]:
        """Query ClinGen's live gene-dosage endpoint. Best-effort; schema not live-verified in this environment."""
        url = f"{self._api_endpoint.rstrip('/')}/{gene_symbol}"
        resp = _api_get(
            url, params={}, timeout=self._timeout, max_retries=self._max_retries, logger=logger
        )
        if resp is None or resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("[ClinGen API] Non-JSON response for gene '%s'", gene_symbol)
            return None

        dosage = data.get("dosageSensitivity") or data.get("dosage") or {}
        if not dosage:
            return None

        return DosageSensitivityRecord(
            gene=gene_symbol,
            haploinsufficiency_score=self._to_int(dosage.get("haploinsufficiencyScore")),
            haploinsufficiency_description=dosage.get("haploinsufficiencyDescription"),
            triplosensitivity_score=self._to_int(dosage.get("triplosensitivityScore")),
            triplosensitivity_description=dosage.get("triplosensitivityDescription"),
            backend_used="api",
        )
