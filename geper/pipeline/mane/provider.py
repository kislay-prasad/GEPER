"""
NCBI MANE (Matched Annotation from NCBI and EBI) Select gene->transcript
lookup.

Reads the local (deployer-provisioned or self-fetched, see
`pipeline/mane/bootstrap.py`) MANE summary TSV, indexed by gene symbol on
first use -- the same local-dataset shape
`pipeline/hpo/provider.py::LocalDatasetHPOProvider` and
`pipeline/clingen/provider.py::LocalDatasetClinGenProvider` already use.

Deliberately no live-API fallback (unlike HPO's `LiveAPIHPOProvider`):
this integration exists specifically to replace a live-per-request
Ensembl dependency that never actually returns MANE data (see
`pipeline/clingen/utils.py`'s module docstring) with a bootstrap-and-
cache dataset -- adding a second live API here would defeat that whole
point. A gene genuinely absent from the MANE dataset (mitochondrial
genes and a handful of others have no MANE Select transcript at all --
confirmed live for MT-ATP8/MT-ATP6) or a dataset that couldn't be
fetched this run both resolve the same honest way: `None`, never a
guess.
"""

from __future__ import annotations

import csv
import os
from threading import Lock
from typing import Dict, Optional

from config import CONFIG
from pipeline.mane import bootstrap as mane_bootstrap
from utils.logger import get_logger

logger = get_logger(__name__)

_MANE_SELECT_STATUS = "MANE Select"


def _normalize_gene_symbol(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper()


def _strip_transcript_version(transcript_id: Optional[str]) -> str:
    """`ENST00000326873.12` -> `ENST00000326873` -- matches the bare,
    unversioned `id` Ensembl's own `lookup/id` REST response uses
    (confirmed live), which is what `TranscriptContext.transcript_id`
    (`pipeline/pvs1/utils.py::transcript_context_from_ensembl`) stores."""
    return (transcript_id or "").split(".")[0]


class LocalDatasetMANEProvider:
    """
    Reads NCBI's MANE summary download from disk, indexed by gene
    symbol on first use. Only "MANE Select" rows are indexed -- "MANE
    Plus Clinical" (a small, distinct category: an additional clinically
    important transcript alongside the gene's own MANE Select one, not
    a substitute for it) is intentionally excluded, since the tie-break
    this feeds (`pipeline/clingen/utils.py::_disambiguate_overlapping_genes`)
    specifically asks "is this candidate gene's own MANE Select
    transcript", not "does MANE mention this transcript at all".
    """

    name = "local_dataset"

    def __init__(self, local_file_path: Optional[str] = None, auto_fetch: bool = True):
        # Same reasoning as `LocalDatasetHPOProvider.__init__`: an
        # explicit path always wins; only when none is given does this
        # reach for a self-fetched copy, and `auto_fetch=False` exists
        # for tests that want a fully offline, deterministic provider.
        self.local_file_path = local_file_path or CONFIG.mane.LOCAL_FILE or None
        self._auto_fetch = auto_fetch and local_file_path is None
        self._lock = Lock()
        self._loaded = False
        self._by_gene_symbol: Dict[str, str] = {}  # gene symbol -> bare MANE Select transcript ID

    def is_available(self) -> bool:
        if not CONFIG.mane.ENABLED:
            return False
        if self.local_file_path:
            return True
        return self._auto_fetch and CONFIG.mane.AUTO_FETCH_ENABLED and not CONFIG.mane.OFFLINE_MODE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # re-check inside the lock
                return
            path = self.local_file_path
            if self._auto_fetch and CONFIG.mane.AUTO_FETCH_ENABLED and not CONFIG.mane.OFFLINE_MODE and not path:
                path = mane_bootstrap.ensure_summary_file()
            if path:
                self._load(path)
            self._loaded = True

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            logger.warning(f"MANE summary local file not found at '{path}'; MANE Select lookups will be empty.")
            return
        count = 0
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                for row in reader:
                    if (row.get("MANE_status") or "").strip() != _MANE_SELECT_STATUS:
                        continue
                    symbol = _normalize_gene_symbol(row.get("symbol"))
                    transcript_id = _strip_transcript_version(row.get("Ensembl_nuc"))
                    if not symbol or not transcript_id:
                        continue
                    self._by_gene_symbol[symbol] = transcript_id
                    count += 1
        except OSError as exc:
            logger.warning(f"Could not read MANE summary file '{path}': {exc}")
            return
        logger.info(f"Loaded {count} MANE Select gene->transcript mapping(s) from '{path}'.")

    def mane_select_transcript_id(self, gene_symbol: str) -> Optional[str]:
        if not self.is_available():
            return None
        self._ensure_loaded()
        return self._by_gene_symbol.get(_normalize_gene_symbol(gene_symbol))


# ---------------------------------------------------------------------------
# Default, process-wide instance -- lazily constructed on first use so
# importing this module never triggers a fetch. `pipeline/clingen/utils.py`
# calls the module-level `mane_select_transcript_id()` below rather than
# constructing its own provider, mirroring how `_disambiguate_overlapping_genes`
# already reuses a single shared `TranscriptLookup` cache rather than a
# fresh instance per call.
# ---------------------------------------------------------------------------

_default_provider: Optional[LocalDatasetMANEProvider] = None
_default_provider_lock = Lock()


def _get_default_provider() -> LocalDatasetMANEProvider:
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = LocalDatasetMANEProvider()
    return _default_provider


def mane_select_transcript_id(gene_symbol: str) -> Optional[str]:
    """
    Bare (unversioned) Ensembl transcript ID of `gene_symbol`'s MANE
    Select transcript, or `None` when the gene has no MANE Select entry
    or the dataset itself is unavailable this run. Never raises --
    callers must degrade to their own existing fallback, never guess.
    """
    return _get_default_provider().mane_select_transcript_id(gene_symbol)
