"""
Self-provisioning of NCBI's official MANE (Matched Annotation from NCBI
and EBI) Select gene->transcript summary download.

Same "download once, query locally, refresh on a TTL" shape as
`pipeline/clingen/bootstrap.py` and `pipeline/hpo/bootstrap.py`: when no
explicit local file is configured, this fetches NCBI's own summary file
once and caches it (decompressed) to disk under
`CONFIG.mane.AUTO_FETCH_DIR`, refreshed on `CONFIG.mane.AUTO_FETCH_TTL_HOURS`.

Two things make this file's fetch shape different from HPO's plain-TSV
download, both verified live during development against
https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/ (2026-08-08):

  1. NCBI's `current/` directory is a stable alias, but the actual
     filenames inside it embed the release version --
     `MANE.GRCh38.v1.5.summary.txt.gz` as of this writing, previously
     `v1.4`, etc. There is no version-agnostic filename to hit
     directly, so `_discover_current_summary_url` fetches and regex-
     parses the directory's own Apache autoindex HTML (a few KB) to
     find the exact current filename, rather than hardcoding a version
     that goes stale at NCBI's next MANE release. The same regex
     capture also yields the version string (e.g. "MANE 1.5") for this
     dataset's provenance record.
  2. The summary file itself is gzip-compressed at the source (`Content-
     Type: application/x-gzip`, confirmed via a live HEAD request) --
     unlike HPO's plain-text download, this is NOT `Content-Encoding:
     gzip` that `requests` would transparently decompress; the response
     body is genuinely gzip bytes, so this module downloads raw bytes
     and gunzips them explicitly before caching a plain TSV.

Confirmed live column header (row 1 of the decompressed file):
`#NCBI_GeneID  Ensembl_Gene  HGNC_ID  symbol  name  RefSeq_nuc
RefSeq_prot  Ensembl_nuc  Ensembl_prot  MANE_status  GRCh38_chr
chr_start  chr_end  chr_strand` -- 19,437 data rows as of v1.5 (19,363
"MANE Select", 74 "MANE Plus Clinical"). `symbol` is the gene symbol;
`Ensembl_nuc` is the (version-suffixed) Ensembl transcript ID, e.g.
"ENST00000326873.12" -- `pipeline/mane/provider.py` strips the version
before indexing, matching the bare (unversioned) `id` Ensembl's own
`lookup/id` REST response uses (confirmed live: `{"id":
"ENST00000326873", "version": 12, ...}`).

Never raises: a failed fetch is logged and reported as "unavailable" so
`pipeline/clingen/utils.py`'s MANE tie-break simply falls back to its
existing AMBIGUOUS outcome, exactly as it did before this module existed.
"""

from __future__ import annotations

import gzip
import os
import re
import tempfile
import time
from threading import Lock
from typing import Optional, Tuple

import requests

from config import CONFIG
from pipeline.provenance import write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_FILENAME = "mane_summary.tsv"

# One lock (there is only ever this one dataset) so two threads racing
# to bootstrap on first use don't both fetch concurrently.
_fetch_lock = Lock()

# Matches an `<a href="MANE.GRCh38.v1.5.summary.txt.gz">` link in NCBI's
# Apache directory-index HTML. Anchored to the `.summary.txt.gz` suffix
# specifically -- the same directory also lists `.gff.gz`/`.gtf.gz`/
# `.faa.gz`/`.fna.gz`/`.gpff.gz`/`.gbff.gz` files this integration has
# no use for (full genomic-feature annotation, not the lightweight
# gene->transcript mapping GEPER needs).
_SUMMARY_FILENAME_RE = re.compile(r'href="(MANE\.GRCh38\.v(\d+\.\d+)\.summary\.txt\.gz)"')


def _cache_dir() -> str:
    return CONFIG.mane.AUTO_FETCH_DIR or os.path.join(CONFIG.CACHE_DIR, "mane")


def summary_cache_path() -> str:
    """Where `ensure_summary_file()` caches its (decompressed) download --
    public so `pipeline/provenance.py`/`pipeline/orchestrator.py` can
    read its provenance sidecar without triggering a fetch."""
    return os.path.join(_cache_dir(), _CACHE_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.mane.AUTO_FETCH_TTL_HOURS


def _discover_current_summary_url(index_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches `index_url` (NCBI's stable `current/` directory listing)
    and regex-parses it for the exact current summary filename and the
    MANE version embedded in it. Returns `(summary_file_url,
    version_string)`, both `None` on any failure (unreachable index,
    unexpected HTML shape) -- never raises.
    """
    try:
        response = requests.get(index_url, timeout=CONFIG.mane.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Could not fetch MANE directory index '{index_url}': {exc}")
        return None, None

    match = _SUMMARY_FILENAME_RE.search(response.text)
    if not match:
        logger.warning(
            f"MANE directory index at '{index_url}' did not contain a recognizable "
            "'MANE.GRCh38.vX.Y.summary.txt.gz' filename; NCBI may have changed its naming scheme."
        )
        return None, None

    filename, version = match.group(1), match.group(2)
    return index_url.rstrip("/") + "/" + filename, f"MANE {version}"


def _download_and_decompress(url: str, dest_path: str, version: Optional[str]) -> bool:
    """
    Fetch the gzip-compressed MANE summary and cache it decompressed
    (plain TSV, so `provider.py` can read it with a plain `csv.DictReader`
    like every other bootstrapped dataset in this codebase). Atomic
    write (temp file + rename), same pattern as
    `pipeline/clingen/bootstrap.py::_download`. Never raises.
    """
    try:
        response = requests.get(url, timeout=CONFIG.mane.AUTO_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        text = gzip.decompress(response.content).decode("utf-8")
        if not text.strip():
            logger.warning(f"MANE summary fetch from '{url}' decompressed to an empty body; not caching.")
            return False
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.warning(f"MANE summary fetch from '{url}' failed: {exc}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".mane_fetch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write MANE summary cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    # Provenance sidecar (pipeline/provenance.py) -- `version` here is a
    # real, source-published release identifier (parsed from the
    # directory listing's own filename, not guessed), so this dataset
    # reaches VERSION_KNOWN status, the same as ClinGen's gene-validity
    # download's Content-Disposition-embedded date.
    write_dataset_provenance_sidecar(dest_path, url, response_headers=response.headers, version=version)
    return True


def ensure_summary_file() -> Optional[str]:
    """Path to a local, decompressed copy of NCBI's MANE Select summary
    download, fetching/refreshing it first if needed. None if unavailable."""
    if not CONFIG.mane.AUTO_FETCH_ENABLED or CONFIG.mane.OFFLINE_MODE:
        return None

    dest_path = summary_cache_path()
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        summary_url, version = _discover_current_summary_url(CONFIG.mane.INDEX_URL)
        if summary_url:
            logger.info(f"Fetching MANE Select summary from '{summary_url}' (cache miss or stale)...")
            if _download_and_decompress(summary_url, dest_path, version):
                logger.info(f"Cached MANE Select summary ({version or 'version unknown'}) to '{dest_path}'.")
                return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (MANE releases roughly every few months; a stale file is
    # far more useful than silently returning "no MANE data" for every
    # gene).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached MANE Select summary at '{dest_path}' after a failed refresh.")
        return dest_path
    return None
