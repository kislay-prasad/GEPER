"""
Self-provisioning of ClinGen's official curated downloads.

`pipeline/clingen/provider.py::LocalDatasetClinGenProvider` already
implements the "download once, query locally" design this codebase
prefers for ClinGen data (see that module's docstring) -- but it has
always required a deployer to manually place a file at
`CONFIG.clingen.GENE_VALIDITY_LOCAL_FILE` / `DOSAGE_SENSITIVITY_LOCAL_FILE`.
Nothing in GEPER ever performed that download itself, and ClinGen's
live per-gene API endpoint that was meant to fill the gap
(`LiveAPIClinGenProvider`) answers with 404 for its documented request
shape (verified against a real request; see that provider's
docstring) -- so out of the box, every gene-disease validity and
dosage-sensitivity lookup silently returned "not found".

This module closes that gap: when no explicit local file is
configured, it fetches ClinGen's own official downloads once and
caches them to disk under `CONFIG.clingen.AUTO_FETCH_DIR`, refreshed
on `CONFIG.clingen.AUTO_FETCH_TTL_HOURS`. Same shape as every other
"fetch once, reuse for the life of a TTL" pattern in this codebase
(`pipeline/*/cache.py`'s `SimpleTTLCache`), just for a whole dataset
file instead of one cached lookup result.

Two real ClinGen endpoints, verified live against clinicalgenome.org
during development (see this package's tests for the frozen fixtures
sliced from these exact responses):

  - Gene-Disease Clinical Validity: `/kb/gene-validity/download`, a
    quoted CSV with a short preamble (title/date/webpage lines and
    "+++"-separator rows) before the real header row -- an
    interactive KB export, not a stable flat-file API, so its
    preamble must be skipped rather than assumed absent.
  - Dosage Sensitivity: the `ftp.clinicalgenome.org` mirror, a
    tab-separated file with its own `#`-prefixed preamble, chosen over
    ClinGen's newer `/kb/gene-dosage/download` KB export because that
    export collapses haploinsufficiency into a text label rather than
    the numeric 0/1/2/3/30/40 score this codebase's dosage model
    (`pipeline/clingen/models.py::DOSAGE_SCORE_LABELS`) and the PVS1
    mechanism gate both key off.

Never raises: a failed fetch is logged and reported as "unavailable"
so callers fall back to the live API exactly as before this module
existed (requirement: never let an optional data source crash the
pipeline).
"""

from __future__ import annotations

import os
import tempfile
import time
from threading import Lock
from typing import Optional

import requests

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

_GENE_VALIDITY_CACHE_FILENAME = "gene_validity.csv"
_DOSAGE_CACHE_FILENAME = "dosage_sensitivity.tsv"

# One lock per dataset so two threads racing to bootstrap on first use
# don't both fetch concurrently; a per-module lock (not per-call) is
# enough since there are only ever these two datasets.
_fetch_lock = Lock()


def _cache_dir() -> str:
    return CONFIG.clingen.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "clingen")


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.clingen.AUTO_FETCH_TTL_HOURS


def _download(url: str, dest_path: str) -> bool:
    """
    Fetch `url` to `dest_path`, atomically (write to a temp file in the
    same directory, then rename) so a reader never sees a
    partially-written cache file if this process is killed mid-download.
    Returns whether it succeeded; never raises.
    """
    try:
        response = requests.get(url, timeout=CONFIG.clingen.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        if not response.text.strip():
            logger.warning(f"ClinGen dataset fetch from '{url}' returned an empty body; not caching.")
            return False
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"ClinGen dataset fetch from '{url}' failed: {exc}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".clingen_fetch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(response.text)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write ClinGen dataset cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False
    return True


def _ensure(url: str, filename: str, label: str) -> Optional[str]:
    if not CONFIG.clingen.AUTO_FETCH_ENABLED or CONFIG.clingen.OFFLINE_MODE:
        return None

    dest_path = os.path.join(_cache_dir(), filename)
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        logger.info(f"Fetching ClinGen {label} from '{url}' (cache miss or stale)...")
        if _download(url, dest_path):
            logger.info(f"Cached ClinGen {label} to '{dest_path}'.")
            return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (ClinGen curation changes slowly; a week-old file is far
    # more useful than silently returning "not found" for every gene).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached ClinGen {label} at '{dest_path}' after a failed refresh.")
        return dest_path
    return None


def ensure_gene_validity_file() -> Optional[str]:
    """Path to a local copy of ClinGen's Gene-Disease Clinical Validity download, fetching/refreshing it first if needed. None if unavailable."""
    return _ensure(CONFIG.clingen.GENE_VALIDITY_DOWNLOAD_URL, _GENE_VALIDITY_CACHE_FILENAME, "gene-disease validity dataset")


def ensure_dosage_sensitivity_file() -> Optional[str]:
    """Path to a local copy of ClinGen's Dosage Sensitivity download, fetching/refreshing it first if needed. None if unavailable."""
    return _ensure(CONFIG.clingen.DOSAGE_SENSITIVITY_DOWNLOAD_URL, _DOSAGE_CACHE_FILENAME, "dosage-sensitivity dataset")
