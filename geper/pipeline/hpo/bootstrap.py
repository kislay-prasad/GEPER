"""
Self-provisioning of HPO's official Gene-to-Phenotype annotation download.

Same "download once, query locally, refresh on a TTL" shape as
`pipeline/clingen/bootstrap.py` -- when no explicit local file is
configured, this fetches HPO's own official
`genes_to_phenotype.txt` release file once and caches it to disk under
`CONFIG.hpo.AUTO_FETCH_DIR`, refreshed on `CONFIG.hpo.AUTO_FETCH_TTL_HOURS`.

Unlike ClinGen's two downloads (which both needed a preamble-skipping
parser -- see that module's docstring), this file is a plain TSV with
a single header row and no preamble, verified live against
http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt during
development (332,600+ rows, header
`ncbi_gene_id  gene_symbol  hpo_id  hpo_name  frequency  disease_id`).

Never raises: a failed fetch is logged and reported as "unavailable" so
callers fall back to the live API (`pipeline/hpo/provider.py::LiveAPIHPOProvider`)
exactly as before this module existed.
"""

from __future__ import annotations

import os
import tempfile
import time
from threading import Lock
from typing import Optional

import requests

from config import CONFIG
from pipeline.provenance import write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_FILENAME = "genes_to_phenotype.txt"

# One lock (there is only ever this one dataset) so two threads racing
# to bootstrap on first use don't both fetch concurrently.
_fetch_lock = Lock()


def _cache_dir() -> str:
    return CONFIG.hpo.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "hpo")


def genes_to_phenotype_cache_path() -> str:
    """Where `ensure_genes_to_phenotype_file()` caches its download --
    public so `pipeline/provenance.py`/`pipeline/orchestrator.py` can
    read its provenance sidecar without triggering a fetch."""
    return os.path.join(_cache_dir(), _CACHE_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.hpo.AUTO_FETCH_TTL_HOURS


def _download(url: str, dest_path: str) -> bool:
    """
    Fetch `url` to `dest_path`, atomically (write to a temp file in the
    same directory, then rename) so a reader never sees a
    partially-written cache file if this process is killed mid-download.
    Returns whether it succeeded; never raises.
    """
    try:
        response = requests.get(url, timeout=CONFIG.hpo.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        if not response.text.strip():
            logger.warning(f"HPO dataset fetch from '{url}' returned an empty body; not caching.")
            return False
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"HPO dataset fetch from '{url}' failed: {exc}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".hpo_fetch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(response.text)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write HPO dataset cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    # Provenance sidecar (pipeline/provenance.py) -- HPO's file carries
    # no in-content or filename-embedded version (verified live: plain
    # TSV, no preamble), so `Last-Modified`/`ETag` plus a content hash
    # are the honest best available signal here, not a fabricated
    # release string.
    write_dataset_provenance_sidecar(dest_path, url, response_headers=response.headers)
    return True


def ensure_genes_to_phenotype_file() -> Optional[str]:
    """Path to a local copy of HPO's Gene-to-Phenotype annotation download, fetching/refreshing it first if needed. None if unavailable."""
    if not CONFIG.hpo.AUTO_FETCH_ENABLED or CONFIG.hpo.OFFLINE_MODE:
        return None

    dest_path = os.path.join(_cache_dir(), _CACHE_FILENAME)
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        logger.info(f"Fetching HPO gene-to-phenotype dataset from '{CONFIG.hpo.DOWNLOAD_URL}' (cache miss or stale)...")
        if _download(CONFIG.hpo.DOWNLOAD_URL, dest_path):
            logger.info(f"Cached HPO gene-to-phenotype dataset to '{dest_path}'.")
            return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (HPO releases roughly monthly; a week-old file is far
    # more useful than silently returning "not found" for every gene).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached HPO dataset at '{dest_path}' after a failed refresh.")
        return dest_path
    return None
