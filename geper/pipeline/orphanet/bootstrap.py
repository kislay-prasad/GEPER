"""
Self-provisioning of Orphanet's official "genes associated with rare
diseases" download (`en_product6.xml`).

Same "download once, query locally, refresh on a TTL" shape as
`pipeline/hpo/bootstrap.py` -- when no explicit local file is
configured, this fetches Orphanet's own official
`https://www.orphadata.com/data/xml/en_product6.xml` release once and
caches it to disk under `CONFIG.orphanet.AUTO_FETCH_DIR`, refreshed on
`CONFIG.orphanet.AUTO_FETCH_TTL_HOURS`.

Unlike HPO/ClinGen, Orphanet has no free live-API fallback to fall
back to when this fetch fails (see `pipeline/orphanet/provider.py`'s
docstring for why: the only self-serve, no-contract-required access to
Orphanet gene-disorder data is this CC BY 4.0 bulk download -- the REST
API is gated behind a paid Data Transfer Agreement/Service Contract).
A failed fetch is therefore reported as "unavailable" with no further
fallback, same as a local-dataset-only provider elsewhere in this
codebase reports it.

Verified live during development: the July 2026 release is ~22MB,
4,245 `<Disorder>` entries (see this module's `utils.py` docstring for
the confirmed schema, and `tests/test_orphanet_live_fetch.py` for the
live download/parse verification).
"""

from __future__ import annotations

import os
import tempfile
import time
from threading import Lock
from typing import Optional

import requests

from config import CONFIG
from pipeline.provenance import record_stale_fallback, write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_FILENAME = "en_product6.xml"

# One lock (there is only ever this one dataset) so two threads racing
# to bootstrap on first use don't both fetch concurrently.
_fetch_lock = Lock()


def _cache_dir() -> str:
    return CONFIG.orphanet.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "orphanet")


def gene_disorder_cache_path() -> str:
    """Where `ensure_gene_disorder_file()` caches its download -- public
    so `pipeline/provenance.py`/`pipeline/orchestrator.py` can read its
    provenance sidecar without triggering a fetch."""
    return os.path.join(_cache_dir(), _CACHE_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.orphanet.AUTO_FETCH_TTL_HOURS


def _download(url: str, dest_path: str) -> bool:
    """
    Fetch `url` to `dest_path`, atomically (write to a temp file in the
    same directory, then rename) so a reader never sees a
    partially-written cache file if this process is killed mid-download.
    Returns whether it succeeded; never raises.
    """
    try:
        response = requests.get(url, timeout=CONFIG.orphanet.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        if not response.content.strip():
            logger.warning(f"Orphanet dataset fetch from '{url}' returned an empty body; not caching.")
            return False
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Orphanet dataset fetch from '{url}' failed: {exc}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".orphanet_fetch_")
    try:
        # Written as bytes (not decoded text) -- the XML declaration's
        # own encoding is what `xml.etree.ElementTree` should honor,
        # not a guess made here.
        with os.fdopen(fd, "wb") as fh:
            fh.write(response.content)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write Orphanet dataset cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    # Provenance sidecar (pipeline/provenance.py). `release_date` is
    # deliberately left unset here: Orphanet's real release date lives
    # INSIDE the XML (the root `<JDBOR date=...>` attribute --
    # `pipeline/orphanet/utils.py::parse_gene_disorder_xml`'s
    # `data_version`), which this function never parses -- it's already
    # surfaced per-query via `OrphanetGeneEvidence.data_version`, so
    # provenance capture reads it from there instead of duplicating the
    # parse here. `Last-Modified`/`ETag` plus a content hash are still
    # recorded as corroborating, restart-surviving metadata.
    write_dataset_provenance_sidecar(dest_path, url, response_headers=response.headers)
    return True


def ensure_gene_disorder_file() -> Optional[str]:
    """Path to a local copy of Orphanet's gene-disorder association download, fetching/refreshing it first if needed. None if unavailable."""
    if not CONFIG.orphanet.AUTO_FETCH_ENABLED or CONFIG.orphanet.OFFLINE_MODE:
        return None

    dest_path = os.path.join(_cache_dir(), _CACHE_FILENAME)
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        logger.info(
            f"Fetching Orphanet gene-disorder dataset from '{CONFIG.orphanet.DOWNLOAD_URL}' (cache miss or stale)..."
        )
        if _download(CONFIG.orphanet.DOWNLOAD_URL, dest_path):
            logger.info(f"Cached Orphanet gene-disorder dataset to '{dest_path}'.")
            return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (Orphanet releases bi-annually, in July and December; a
    # few-month-old file is far more useful than silently returning
    # "not found" for every gene).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached Orphanet dataset at '{dest_path}' after a failed refresh.")
        record_stale_fallback(source="Orphanet", path=dest_path, reason="failed refresh")
        return dest_path
    return None
