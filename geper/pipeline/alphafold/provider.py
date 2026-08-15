"""
AlphaFold DB query providers.

Mirrors `pipeline/interpro/provider.py`'s shape: a local-dataset
provider for offline/deterministic use, a live API provider for
AlphaFold DB's public prediction API (`alphafold.ebi.ac.uk/api`) plus
an optional structure-file download for per-residue pLDDT, and a
composite that tries local first then the live API, honoring offline
mode. Never raises out of `query()`.

Two network calls make up the live path (see `LiveAPIAlphaFoldProvider`):
  1. The summary/metadata API (small JSON: model URLs, version,
     UniProt coordinate range).
  2. The structure file itself (PDB format), only if
     `CONFIG.alphafold.FETCH_STRUCTURE_FILE` is on -- this is where
     per-residue pLDDT actually lives (in the B-factor column; see
     `pipeline/alphafold/utils.py::parse_pdb_plddt`), and is a bigger
     network/time cost than step 1, so it's independently toggleable
     and its own result is cached separately (keyed by accession, held
     only in-process/in-memory -- a full per-residue array is not
     something this codebase persists to the on-disk JSON-lines cache
     `AlphaFoldCache` uses for the small summary result, to avoid an
     unbounded-size disk cache entry for large proteins).

NOTE (same sandbox caveat documented in `pipeline/interpro/provider.py`
and `pipeline/clingen/provider.py`): this sandbox has no network route
to `alphafold.ebi.ac.uk`, so the live path is implemented against
AlphaFold DB's officially published API/file-format documentation
(https://alphafold.ebi.ac.uk/api-docs, https://alphafold.ebi.ac.uk/faq)
and exercised in tests with `requests` mocked out. Re-verify against a
live call before relying on it in production.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional

import requests

from config import CONFIG
from pipeline.alphafold.models import AlphaFoldAnnotation, confidence_band
from pipeline.alphafold.utils import normalize_accession, parse_pdb_plddt
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class AlphaFoldProviderBase:
    """Common interface every AlphaFold DB data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, accession: str, protein_position: Optional[int] = None) -> Optional[AlphaFoldAnnotation]:
        """Return an AlphaFoldAnnotation, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


class LocalDatasetAlphaFoldProvider(AlphaFoldProviderBase):
    """Queries a local, accession-indexed JSON-lines dataset of pre-fetched AlphaFold DB summaries (+ optional per-residue pLDDT)."""

    name = "local_dataset"

    def __init__(self, dataset_path: Optional[str] = None):
        self.dataset_path = dataset_path
        self._index: Optional[Dict[str, Dict[str, Any]]] = None

    def is_available(self) -> bool:
        return bool(self.dataset_path) and os.path.exists(self.dataset_path)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._index is not None:
            return self._index
        index: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self.dataset_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    accession = normalize_accession(record.get("accession"))
                    if accession:
                        index[accession] = record
        except OSError as exc:
            logger.warning(f"Could not read local AlphaFold dataset '{self.dataset_path}': {exc}")
        self._index = index
        return index

    def query(self, accession: str, protein_position: Optional[int] = None) -> Optional[AlphaFoldAnnotation]:
        if not self.is_available():
            return None
        key = normalize_accession(accession)
        record = self._load().get(key or "")
        if record is None:
            return AlphaFoldAnnotation.not_found(accession, self.name)

        residue_plddt = {int(k): float(v) for k, v in (record.get("residue_plddt") or {}).items()}
        return _build_annotation(
            accession=accession,
            source=self.name,
            summary=record.get("summary") or {},
            residue_plddt=residue_plddt,
            protein_position=protein_position,
            structure_fetched=bool(residue_plddt),
        )


class LiveAPIAlphaFoldProvider(AlphaFoldProviderBase):
    """Queries AlphaFold DB's public prediction API, and (optionally) downloads the structure file for per-residue pLDDT."""

    name = "alphafold_db_api"

    def __init__(self, api_base: Optional[str] = None):
        self.api_base = api_base or CONFIG.alphafold.API_BASE
        # Small, bounded, in-memory-only cache of the full per-residue
        # pLDDT array -- deliberately separate from `AlphaFoldCache`
        # (which persists the much smaller per-variant summary result
        # to disk); a multi-thousand-residue array isn't something
        # this codebase writes to the JSON-lines disk cache format.
        self._structure_cache: "OrderedDict[str, Dict[int, float]]" = OrderedDict()
        self._structure_cache_lock = Lock()
        self._structure_cache_max_size = 200

    def is_available(self) -> bool:
        return bool(CONFIG.alphafold.ENABLED) and not CONFIG.alphafold.OFFLINE_MODE

    def query(self, accession: str, protein_position: Optional[int] = None) -> Optional[AlphaFoldAnnotation]:
        if not self.is_available():
            return None

        normalized = normalize_accession(accession)
        if not normalized:
            return AlphaFoldAnnotation.from_error(accession, "no UniProt accession provided")

        try:
            entries = self._get_json(f"{self.api_base}/{normalized}")
        except ExternalAPIError as exc:
            logger.warning(f"AlphaFold DB summary query failed for accession '{normalized}': {exc}")
            return AlphaFoldAnnotation.from_error(accession, str(exc))

        if not entries:
            return AlphaFoldAnnotation.not_found(accession, self.name)

        summary = entries[0]  # first fragment/model, matching AlphaFold DB's own default entry-page choice

        residue_plddt: Dict[int, float] = {}
        structure_fetched = False
        if CONFIG.alphafold.FETCH_STRUCTURE_FILE and summary.get("pdbUrl"):
            residue_plddt = self._get_structure_plddt(normalized, summary["pdbUrl"])
            structure_fetched = bool(residue_plddt)

        return _build_annotation(
            accession=accession,
            source=self.name,
            summary=summary,
            residue_plddt=residue_plddt,
            protein_position=protein_position,
            structure_fetched=structure_fetched,
        )

    def _get_structure_plddt(self, accession: str, pdb_url: str) -> Dict[int, float]:
        with self._structure_cache_lock:
            cached = self._structure_cache.get(accession)
            if cached is not None:
                self._structure_cache.move_to_end(accession)
                return cached

        try:
            response = requests.get(pdb_url, timeout=CONFIG.alphafold.QUERY_TIMEOUT_SECS, stream=True)
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > CONFIG.alphafold.MAX_STRUCTURE_FILE_BYTES:
                logger.warning(
                    f"AlphaFold structure file for '{accession}' is {content_length} bytes, exceeding "
                    f"the {CONFIG.alphafold.MAX_STRUCTURE_FILE_BYTES}-byte limit; skipping per-residue pLDDT for this protein."
                )
                return {}
            pdb_text = response.text
        except requests.RequestException as exc:
            logger.warning(f"Could not download AlphaFold structure file for '{accession}': {exc}")
            return {}

        residue_plddt = parse_pdb_plddt(pdb_text)
        with self._structure_cache_lock:
            self._structure_cache[accession] = residue_plddt
            self._structure_cache.move_to_end(accession)
            while len(self._structure_cache) > self._structure_cache_max_size:
                self._structure_cache.popitem(last=False)
        return residue_plddt

    def _get_json(self, url: str) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.alphafold.MAX_RETRIES + 1):
            try:
                response = requests.get(url, timeout=CONFIG.alphafold.QUERY_TIMEOUT_SECS)
                if response.status_code == 404:
                    return []  # AlphaFold DB returns a plain 404 for an accession with no predicted structure
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"AlphaFold DB API request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.alphafold.MAX_RETRIES:
                    time.sleep(CONFIG.alphafold.RETRY_BACKOFF_SECS * attempt)
        logger.warning(
            f"AlphaFold DB API request to '{url}' failed after {CONFIG.alphafold.MAX_RETRIES} attempts: {last_error}"
        )
        # Round 24: same rationale as `pipeline/uniprot/provider.py`'s
        # sibling fix -- no url/last_error in what's raised (folded
        # verbatim into `struct['error']`, rendered unconditionally and
        # embedded in geper_results.json), only in the log line above.
        raise ExternalAPIError(f"AlphaFold DB API request failed after {CONFIG.alphafold.MAX_RETRIES} attempts")


def _build_annotation(
    accession: str,
    source: str,
    summary: Dict[str, Any],
    residue_plddt: Dict[int, float],
    protein_position: Optional[int],
    structure_fetched: bool,
) -> AlphaFoldAnnotation:
    mean_plddt = (sum(residue_plddt.values()) / len(residue_plddt)) if residue_plddt else None

    affected_plddt = residue_plddt.get(protein_position) if (protein_position is not None and residue_plddt) else None

    thresholds = dict(
        very_high=CONFIG.alphafold.PLDDT_VERY_HIGH_THRESHOLD,
        confident=CONFIG.alphafold.PLDDT_CONFIDENT_THRESHOLD,
        low=CONFIG.alphafold.PLDDT_LOW_THRESHOLD,
    )

    return AlphaFoldAnnotation(
        accession=accession,
        source=source,
        found=True,
        model_version=str(summary.get("latestVersion")) if summary.get("latestVersion") is not None else None,
        pdb_url=summary.get("pdbUrl"),
        cif_url=summary.get("cifUrl"),
        uniprot_start=summary.get("uniprotStart"),
        uniprot_end=summary.get("uniprotEnd"),
        mean_plddt=mean_plddt,
        mean_plddt_band=confidence_band(mean_plddt, **thresholds),
        protein_position=protein_position,
        affected_residue_plddt=affected_plddt,
        affected_residue_band=confidence_band(affected_plddt, **thresholds),
        protein_position_basis="transcript_cds" if protein_position is not None else None,
        structure_fetched=structure_fetched,
    )


class CompositeAlphaFoldProvider:
    """Tries the local dataset first (when configured), then AlphaFold DB's live API, honoring offline mode. Never raises."""

    def __init__(
        self,
        local_provider: Optional[LocalDatasetAlphaFoldProvider] = None,
        api_provider: Optional[LiveAPIAlphaFoldProvider] = None,
    ):
        self.local_provider = local_provider or LocalDatasetAlphaFoldProvider(
            dataset_path=CONFIG.alphafold.LOCAL_DATASET_FILE or None
        )
        self.api_provider = api_provider or LiveAPIAlphaFoldProvider()

    def query(self, accession: str, protein_position: Optional[int] = None) -> AlphaFoldAnnotation:
        if CONFIG.alphafold.OFFLINE_MODE:
            result = self._try(self.local_provider, accession, protein_position)
            if result is not None:
                return result
            return AlphaFoldAnnotation.from_error(accession, "offline mode: no local AlphaFold dataset configured")

        result = self._try(self.local_provider, accession, protein_position)
        if result is not None:
            return result

        result = self._try(self.api_provider, accession, protein_position)
        if result is not None:
            return result

        return AlphaFoldAnnotation.not_found(accession, "none")

    @staticmethod
    def _try(
        provider: AlphaFoldProviderBase, accession: str, protein_position: Optional[int]
    ) -> Optional[AlphaFoldAnnotation]:
        try:
            return provider.query(accession, protein_position)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AlphaFold provider '{provider.name}' raised unexpectedly: {exc}")
            return AlphaFoldAnnotation.from_error(accession, f"{provider.name} raised: {exc}")
