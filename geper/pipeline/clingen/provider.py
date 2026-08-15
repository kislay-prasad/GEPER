"""
ClinGen query providers.

Two concrete sources, matching requirement #3 ("Official ClinGen APIs.
Official downloadable datasets where available."):

  - `LocalDatasetClinGenProvider`: reads ClinGen's own published,
    versioned flat-file downloads (Gene-Disease Validity + Dosage
    Sensitivity TSV/CSV, from
    https://search.clinicalgenome.org/kb/gene-validity and
    https://search.clinicalgenome.org/kb/dosage/download) that a
    deployer provisions locally, exactly the same "download once,
    query locally, no live network dependency" shape as
    `pipeline/gnomad/provider.py::LocalIndexedGnomadProvider` for
    gnomAD's site VCFs. This is the *primary* source: ClinGen curation
    changes on the order of weeks/months, not per-request, so a
    periodically-refreshed local copy is both faster and more
    reliable than a live call for routine annotation.
  - `LiveAPIClinGenProvider`: an HTTP fallback for genes not present in
    the local dataset (e.g. a curation newer than the last local
    refresh), with retry/backoff/timeout matching every other external
    client in this codebase (`database/clinvar_client.py::_request_json`,
    `pipeline/gnomad/provider.py::GraphQLGnomadProvider._post`).

    IMPORTANT / KNOWN LIMITATION: this environment has no network
    route to clinicalgenome.org (see `README.md`'s ClinGen section),
    so the exact request/response shape below could not be executed
    against ClinGen's live API during development. The endpoint
    template and response-field names are ClinGen's documented
    Linked Data Hub (LDH) convention as of ClinGen's public API
    documentation, but should be re-verified against a live call
    before this fallback is relied on in production -- set
    `GEPER_CLINGEN_API_ENABLED=false` (the default is `true` but
    `is_available()` also honors `OFFLINE_MODE`) to disable it
    entirely and rely solely on the local dataset until verified.

`CompositeClinGenProvider` tries the local dataset first, then the
live API, honoring offline mode, and never raises out of
`query()`/`batch_query()` -- callers always get a `ClinGenGeneEvidence`
back (possibly with `.error` set), matching this codebase's
graceful-degradation policy for every external evidence source.
"""

from __future__ import annotations

import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.clingen import bootstrap as clingen_bootstrap
from pipeline.clingen.models import (
    Actionability,
    ClinGenGeneEvidence,
    DosageSensitivity,
    GeneDiseaseValidity,
)
from pipeline.clingen.utils import normalize_gene_symbol, parse_dosage_row, parse_gene_validity_row
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class ClinGenProviderBase:
    """Common interface every ClinGen data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, gene_symbol: str) -> Optional[ClinGenGeneEvidence]:
        """Return a ClinGenGeneEvidence, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local curated-dataset provider
# ---------------------------------------------------------------------------


class LocalDatasetClinGenProvider(ClinGenProviderBase):
    """
    Reads ClinGen's own Gene-Disease Validity and Dosage Sensitivity
    flat-file downloads from disk, indexed by gene symbol on first
    use. Both files are optional and independent: a deployment can
    provision either, both, or neither (in which case this provider
    reports itself unavailable and `CompositeClinGenProvider` falls
    through to the live API).

    Both files are parsed once, lazily, and cached in memory for the
    life of the process (a TSV of ClinGen's curated genes is a few
    thousand rows -- small enough to hold entirely in memory, same
    tradeoff AlphaMissense's tabix-indexed catalogue makes the
    opposite way only because that catalogue is genome-wide and far
    larger).
    """

    name = "local_dataset"

    def __init__(
        self,
        gene_validity_path: Optional[str] = None,
        dosage_sensitivity_path: Optional[str] = None,
        auto_fetch: bool = True,
    ):
        # Explicit paths (constructor arg or CONFIG's deployer-pinned
        # file) always win. Only when neither is given does this reach
        # for a self-fetched copy of ClinGen's own download -- see
        # `pipeline/clingen/bootstrap.py`'s docstring for why that
        # matters: without it, `is_available()` below is False on
        # every out-of-the-box install and every lookup silently
        # returns "not found". `auto_fetch=False` exists for tests that
        # want a fully offline, deterministic provider (see
        # tests/test_clingen_gene_validity.py) without needing to
        # override CONFIG.clingen.AUTO_FETCH_ENABLED globally.
        self.gene_validity_path = gene_validity_path or CONFIG.clingen.GENE_VALIDITY_LOCAL_FILE or None
        self.dosage_sensitivity_path = dosage_sensitivity_path or CONFIG.clingen.DOSAGE_SENSITIVITY_LOCAL_FILE or None
        self._auto_fetch = auto_fetch and gene_validity_path is None and dosage_sensitivity_path is None
        self._lock = Lock()
        self._loaded = False
        self._validity_by_gene: Dict[str, List[GeneDiseaseValidity]] = {}
        self._dosage_by_gene: Dict[str, DosageSensitivity] = {}

    def is_available(self) -> bool:
        if self.gene_validity_path or self.dosage_sensitivity_path:
            return True
        return self._auto_fetch and CONFIG.clingen.AUTO_FETCH_ENABLED and not CONFIG.clingen.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock (another thread may have just finished)
                return
            gene_validity_path = self.gene_validity_path
            dosage_sensitivity_path = self.dosage_sensitivity_path
            if self._auto_fetch and CONFIG.clingen.AUTO_FETCH_ENABLED and not CONFIG.clingen.OFFLINE_MODE:
                if not gene_validity_path:
                    gene_validity_path = clingen_bootstrap.ensure_gene_validity_file()
                if not dosage_sensitivity_path:
                    dosage_sensitivity_path = clingen_bootstrap.ensure_dosage_sensitivity_file()
            if gene_validity_path:
                self._load_gene_validity(gene_validity_path)
            if dosage_sensitivity_path:
                self._load_dosage_sensitivity(dosage_sensitivity_path)
            self._loaded = True

    def _load_gene_validity(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(
                f"ClinGen gene-validity local file not found at '{path}'; local dataset lookups for gene-disease validity will be empty."
            )
            return
        count = 0
        try:
            for row in _clingen_export_rows(path):
                parsed = parse_gene_validity_row(row)
                if parsed is None:
                    continue
                self._validity_by_gene.setdefault(parsed.gene_symbol, []).append(parsed)
                count += 1
        except OSError as exc:
            logger.warning(f"Could not read ClinGen gene-validity file '{path}': {exc}")
            return
        logger.info(f"Loaded {count} ClinGen gene-disease validity curation(s) from '{path}'.")

    def _load_dosage_sensitivity(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(
                f"ClinGen dosage-sensitivity local file not found at '{path}'; local dataset lookups for dosage sensitivity will be empty."
            )
            return
        count = 0
        try:
            for row in _clingen_export_rows(path):
                parsed = parse_dosage_row(row)
                if parsed is None:
                    continue
                # One row per gene in ClinGen's dosage download; a
                # duplicate gene symbol (re-curation) keeps the
                # last row encountered, matching the file's own
                # "most recent row wins" convention.
                self._dosage_by_gene[parsed.gene_symbol] = parsed
                count += 1
        except OSError as exc:
            logger.warning(f"Could not read ClinGen dosage-sensitivity file '{path}': {exc}")
            return
        logger.info(f"Loaded {count} ClinGen dosage-sensitivity curation(s) from '{path}'.")

    def query(self, gene_symbol: str) -> Optional[ClinGenGeneEvidence]:
        if not self.is_available():
            return None
        self._ensure_loaded()

        gene_symbol = normalize_gene_symbol(gene_symbol)
        validities = self._validity_by_gene.get(gene_symbol, [])
        dosage = self._dosage_by_gene.get(gene_symbol)

        if not validities and dosage is None:
            return ClinGenGeneEvidence.not_found(gene_symbol, self.name)

        # ClinGen's own Gene-Disease Validity download carries a
        # "GENE ID (HGNC)" column (parsed into `GeneDiseaseValidity.gene_id`
        # by `parse_gene_validity_row`); the Dosage Sensitivity download does
        # not carry a gene-id column at all. Surface whichever HGNC id is
        # available from the validity rows as this evidence's
        # `clingen_gene_id` -- previously this was left unset here (always
        # `None`) even when the local file provided one, which is the
        # defect this fixes.
        gene_id = next((v.gene_id for v in validities if v.gene_id), None)

        return ClinGenGeneEvidence(
            gene_symbol=gene_symbol,
            source=self.name,
            found=True,
            gene_disease_validities=validities,
            dosage_sensitivity=dosage,
            clingen_gene_id=gene_id,
        )


def _sniff_delimiter(path: str) -> str:
    """ClinGen publishes both comma- and tab-separated download variants; infer from the file extension."""
    return "\t" if path.lower().endswith((".tsv", ".txt")) else ","


def _clingen_export_rows(path: str) -> "List[Dict[str, str]]":
    """
    Parse a real ClinGen curated-download file into `csv.DictReader`-
    style row dicts, tolerating the preamble both live export formats
    actually carry.

    Neither of ClinGen's two real download endpoints this codebase
    fetches is a bare header-then-data flat file (verified live; see
    `pipeline/clingen/bootstrap.py`'s docstring):
      - the Gene-Disease Validity KB export opens with three
        title/date/URL lines, then a "+++"-only separator row, then
        the real header row, then another separator row before data
        starts;
      - the Dosage Sensitivity ftp mirror opens with five `#`-prefixed
        title/date/note lines, the last of which *is* the real header
        row (also `#`-prefixed).

    A bare `csv.DictReader(fh)` -- what this function replaces -- treats
    line 1 as the header in both cases, silently misparsing every row
    (this was a real defect: the gene-validity and dosage-sensitivity
    local-dataset paths never actually parsed a live ClinGen download
    correctly). This instead scans for the header by content (the
    first line whose first cell, once stripped of a leading '#' and
    surrounding quotes/whitespace, reads "gene symbol" case-
    insensitively) rather than assuming its position, so it tolerates
    either preamble shape -- or none, for a deployer-authored flat
    file that never had one to begin with.
    """
    delimiter = _sniff_delimiter(path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    header_idx = None
    header_cells: List[str] = []
    for idx, line in enumerate(lines):
        cells = next(csv.reader([line], delimiter=delimiter), [])
        if cells and _normalize_header_cell(cells[0]).lower() == "gene symbol":
            header_idx = idx
            header_cells = [_normalize_header_cell(c) if i == 0 else c.strip() for i, c in enumerate(cells)]
            break
    if header_idx is None:
        return []  # no recognizable header at all -- not a ClinGen export this parser understands

    rows: List[Dict[str, str]] = []
    for line in lines[header_idx + 1 :]:
        cells = next(csv.reader([line], delimiter=delimiter), [])
        if not cells or not cells[0].strip():
            continue
        if all(set(c.strip()) <= {"+"} for c in cells if c.strip()):
            continue  # the "+++"-only separator row the KB export repeats after its header
        rows.append(dict(zip(header_cells, cells)))
    return rows


def _normalize_header_cell(cell: str) -> str:
    """Strip a leading '#' (the ftp export's convention) and surrounding whitespace from one header cell."""
    return cell.strip().lstrip("#").strip()


# ---------------------------------------------------------------------------
# Live API fallback
# ---------------------------------------------------------------------------


class LiveAPIClinGenProvider(ClinGenProviderBase):
    """
    Queries ClinGen's live gene-curation API as a fallback when a gene
    isn't present in the local dataset (or no local dataset is
    configured at all). See this module's docstring for the important
    caveat about this provider's response schema not having been
    verified against a live call in this environment.
    """

    name = "api"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = endpoint or CONFIG.clingen.API_ENDPOINT

    def is_available(self) -> bool:
        return bool(CONFIG.clingen.API_ENABLED) and not CONFIG.clingen.OFFLINE_MODE

    def query(self, gene_symbol: str) -> Optional[ClinGenGeneEvidence]:
        if not self.is_available():
            return None
        gene_symbol = normalize_gene_symbol(gene_symbol)
        try:
            payload = self._get(gene_symbol)
        except ExternalAPIError as exc:
            logger.warning(f"ClinGen API query failed for gene '{gene_symbol}': {exc}")
            return ClinGenGeneEvidence.from_error(gene_symbol, str(exc))

        if not payload:
            return ClinGenGeneEvidence.not_found(gene_symbol, self.name)

        return _evidence_from_api_payload(gene_symbol, payload, self.name)

    def _get(self, gene_symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.endpoint.rstrip('/')}/gene/{gene_symbol}"
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.clingen.MAX_RETRIES + 1):
            try:
                response = requests.get(url, timeout=CONFIG.clingen.QUERY_TIMEOUT_SECS)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"ClinGen API request attempt {attempt} failed for '{gene_symbol}': {exc}")
                if attempt < CONFIG.clingen.MAX_RETRIES:
                    time.sleep(CONFIG.clingen.RETRY_BACKOFF_SECS * attempt)
        logger.warning(
            f"ClinGen API request to '{url}' failed after {CONFIG.clingen.MAX_RETRIES} attempts: {last_error}"
        )
        # Round 24: same rationale as `pipeline/uniprot/provider.py`'s
        # sibling fix -- no url/last_error in what's raised (folded
        # verbatim into `clingen_error`, rendered unconditionally and
        # embedded in geper_results.json), only in the log line above.
        raise ExternalAPIError(f"ClinGen API request failed after {CONFIG.clingen.MAX_RETRIES} attempts")


def _evidence_from_api_payload(gene_symbol: str, payload: Dict[str, Any], source: str) -> ClinGenGeneEvidence:
    """
    Map a ClinGen gene-curation API JSON payload to `ClinGenGeneEvidence`.

    Field names below follow ClinGen's documented gene-validity/dosage
    JSON conventions (`geneValidityCurations`, `dosageSensitivity`,
    `actionabilityCurations`, `clingenGeneId` / `@id`); see this
    module's docstring for the caveat that this mapping has not been
    exercised against a live response in this environment and should
    be spot-checked against a real payload before production use.
    """
    validities = []
    for entry in payload.get("geneValidityCurations", []) or []:
        validities.append(
            GeneDiseaseValidity(
                gene_symbol=gene_symbol,
                disease_label=entry.get("diseaseLabel") or entry.get("disease_label") or "Unknown disease",
                disease_id=entry.get("diseaseId") or entry.get("disease_id"),
                classification=entry.get("classification"),
                moi=entry.get("moi") or entry.get("modeOfInheritance"),
                sop_version=entry.get("sopVersion") or entry.get("sop"),
                gcep=entry.get("gcep") or entry.get("curatingGroup"),
                classification_date=entry.get("classificationDate") or entry.get("date"),
                online_report_url=entry.get("reportUrl") or entry.get("onlineReport"),
            )
        )

    dosage = None
    dosage_payload = payload.get("dosageSensitivity") or payload.get("dosage")
    if dosage_payload:
        dosage = DosageSensitivity(
            gene_symbol=gene_symbol,
            haploinsufficiency_score=_safe_int(dosage_payload.get("haploinsufficiencyScore")),
            haploinsufficiency_description=dosage_payload.get("haploinsufficiencyDescription"),
            triplosensitivity_score=_safe_int(dosage_payload.get("triplosensitivityScore")),
            triplosensitivity_description=dosage_payload.get("triplosensitivityDescription"),
        )

    actionability = []
    for entry in payload.get("actionabilityCurations", []) or []:
        actionability.append(
            Actionability(
                gene_symbol=gene_symbol,
                disease_label=entry.get("diseaseLabel"),
                adult_actionability_score=entry.get("adultScore"),
                pediatric_actionability_score=entry.get("pediatricScore"),
                report_url=entry.get("reportUrl"),
            )
        )

    return ClinGenGeneEvidence(
        gene_symbol=gene_symbol,
        source=source,
        found=True,
        gene_disease_validities=validities,
        dosage_sensitivity=dosage,
        actionability=actionability,
        clingen_gene_id=payload.get("clingenGeneId") or payload.get("@id"),
        last_updated=payload.get("lastUpdated") or payload.get("last_updated"),
    )


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Composite: local-first, API-fallback, sync + async batch
# ---------------------------------------------------------------------------


class CompositeClinGenProvider:
    """
    Tries the local curated-dataset provider first (when configured),
    then the live API fallback (unless offline mode is on) --
    requirement #3's "Official downloadable datasets" preferred over
    "Official ClinGen APIs" for routine use, exactly the same
    local-first/API-fallback shape as
    `pipeline/gnomad/provider.py::CompositeGnomadProvider`. Never
    raises: any provider failure is logged and reported inside the
    returned `ClinGenGeneEvidence.error`.
    """

    def __init__(
        self,
        local_provider: Optional[LocalDatasetClinGenProvider] = None,
        api_provider: Optional[LiveAPIClinGenProvider] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalDatasetClinGenProvider()
        self.api_provider = api_provider or LiveAPIClinGenProvider()
        self.max_concurrent = max_concurrent or CONFIG.clingen.MAX_CONCURRENT

    def query(self, gene_symbol: str) -> ClinGenGeneEvidence:
        gene_symbol = normalize_gene_symbol(gene_symbol)
        if not gene_symbol:
            return ClinGenGeneEvidence.from_error("", "no gene symbol provided")

        if CONFIG.clingen.OFFLINE_MODE:
            result = self._try(self.local_provider, gene_symbol)
            if result is not None:
                return result
            return ClinGenGeneEvidence.from_error(
                gene_symbol, "offline mode: no local ClinGen dataset configured (or gene not present in it)"
            )

        result = self._try(self.local_provider, gene_symbol)
        if result is not None and result.found:
            return result

        api_result = self._try(self.api_provider, gene_symbol)
        if api_result is not None and (api_result.found or api_result.error):
            return api_result

        # Neither source found the gene (as opposed to erroring) --
        # prefer returning the local provider's clean "not found" over
        # a None if it answered at all, so callers can distinguish "we
        # checked and there's no ClinGen curation for this gene" from
        # "no source could be reached".
        if result is not None:
            return result
        if api_result is not None:
            return api_result
        return ClinGenGeneEvidence.not_found(gene_symbol, "none")

    @staticmethod
    def _try(provider: ClinGenProviderBase, gene_symbol: str) -> Optional[ClinGenGeneEvidence]:
        try:
            return provider.query(gene_symbol)
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            logger.error(f"ClinGen provider '{provider.name}' raised unexpectedly: {exc}")
            return ClinGenGeneEvidence.from_error(gene_symbol, f"{provider.name} raised: {exc}")

    # -- batch: thread-pooled -------------------------------------------

    def batch_query(self, gene_symbols: List[str]) -> List[ClinGenGeneEvidence]:
        """Concurrent (thread-pooled, since each query is I/O-bound: a file lookup or HTTP round trip)."""
        if not gene_symbols:
            return []
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            return list(executor.map(self.query, gene_symbols))
