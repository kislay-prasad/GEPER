"""
Self-provisioning of UniProt's official human reference proteome
download, converted into the JSON-lines dataset shape
`pipeline/uniprot/provider.py::LocalDatasetUniProtProvider` already
expects (`{"gene_symbol": "...", "entry": {...UniProt REST API entry
JSON...}}` per line).

Same "download once, query locally, refresh on a TTL" shape as
`pipeline/mane/bootstrap.py` and `pipeline/hpo/bootstrap.py`. Two things
make this source's fetch shape different from either of those:

  1. UniProt's `current_release` directory (confirmed live 2026-08-08:
     https://ftp.uniprot.org/pub/databases/uniprot/current_release/
     knowledgebase/reference_proteomes/Eukaryota/UP000005640/) is a
     stable alias whose filenames are NOT version-suffixed (unlike
     NCBI's MANE directory, where the version is embedded in the
     filename itself) -- `UP000005640_9606.dat.gz` always names the
     same file, just with different content release-to-release. So no
     directory-listing regex-discovery step is needed here. UniProt
     instead publishes a clean `RELEASE.metalink` XML manifest right
     alongside the data file, with an explicit `<version>` tag
     (confirmed live: "2026_02") -- `_discover_version` reads that
     directly rather than parsing HTML.
  2. The data file itself is UniProt's Swiss-Prot flat-file format
     (`.dat`), NOT the REST API's JSON entry shape `LocalDatasetUniProtProvider`
     expects to read -- so this module doesn't just decompress-and-cache
     like MANE/HPO did, it *parses and reshapes* each record via
     Biopython's `Bio.SwissProt` parser (already a hard dependency of
     this codebase -- see `database/blast_client.py`) into the same
     field names `pipeline/uniprot/utils.py::parse_uniprot_entry`
     already reads from a live REST API response, so
     `LocalDatasetUniProtProvider` needs zero changes to consume it.

Only the reference proteome's `.dat.gz` file (~128MB compressed) is
fetched -- the accompanying `.gene2acc.gz` mapping file was
investigated and is NOT needed: it carries only accession<->external-ID
cross-references (no function/disease/feature annotation), and the
gene symbol needed to index each record by is already present in the
`.dat` file's own GN (gene name) line.

Streamed end to end (download to a temp `.dat.gz` file on disk, then
`Bio.SwissProt.parse` reads and converts one record at a time) so this
never holds the full ~600MB+ decompressed flat file in memory at once
-- important on this codebase's target 8GB-RAM deployment profile.

Never raises: a failed fetch/parse is logged and reported as
"unavailable" so `CompositeUniProtProvider` falls back to the live
REST API exactly as before this module existed.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from threading import Lock
from typing import Any, Dict, List, Optional

import requests
from Bio import SwissProt

from config import CONFIG
from pipeline.provenance import record_stale_fallback, write_dataset_provenance_sidecar
from utils.logger import get_logger

logger = get_logger(__name__)

_DATASET_FILENAME = "uniprot_local_dataset.jsonl"
_RAW_DOWNLOAD_FILENAME = "UP000005640_9606.dat.gz"

# One lock (there is only ever this one dataset) so two threads racing
# to bootstrap on first use don't both fetch/parse concurrently.
_fetch_lock = Lock()

_METALINK_NS = {"m": "http://www.metalinker.org/"}

# UniProt flat-file `DE   RecName: Full=...;` line, already reassembled
# into one string by Bio.SwissProt as `record.description`. Evidence
# tags (`{ECO:...}`) are stripped -- they're citation metadata, not
# part of the protein name itself, and `parse_uniprot_entry` never
# expects them (the live REST API's `fullName.value` doesn't carry
# them either).
_RECNAME_RE = re.compile(r"RecName:\s*Full=([^;{]+)")
_SUBMITTED_NAME_RE = re.compile(r"SubName:\s*Full=([^;{]+)")

# `record.organism` is a human-readable string like "Homo sapiens
# (Human)." -- the REST API's `organism.scientificName` field is just
# the binomial name with no common-name parenthetical or trailing
# punctuation.
_ORGANISM_RE = re.compile(r"^([^(.]+)")


def _cache_dir() -> str:
    return CONFIG.uniprot.AUTO_FETCH_LOCAL_DIR or os.path.join(CONFIG.CACHE_DIR, "uniprot")


def dataset_cache_path() -> str:
    """Where `ensure_dataset_file()` caches its converted JSON-lines
    dataset -- public so `pipeline/provenance.py`/`pipeline/orchestrator.py`
    can read its provenance sidecar without triggering a fetch."""
    return os.path.join(_cache_dir(), _DATASET_FILENAME)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
    return age_hours < CONFIG.uniprot.AUTO_FETCH_TTL_HOURS


def _discover_version(dir_url: str) -> Optional[str]:
    """Reads `RELEASE.metalink`'s `<version>` tag from UniProt's stable
    `current_release` directory (e.g. "2026_02"). Returns None on any
    failure -- never raises, and a missing version doesn't block the
    fetch itself (see `ensure_dataset_file`), only degrades the
    provenance record from VERSION_KNOWN to a lesser status."""
    try:
        response = requests.get(
            dir_url.rstrip("/") + "/RELEASE.metalink", timeout=CONFIG.uniprot.AUTO_FETCH_TIMEOUT_SECS
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        version_el = root.find("m:version", _METALINK_NS)
        return version_el.text.strip() if version_el is not None and version_el.text else None
    except (requests.RequestException, ET.ParseError) as exc:
        logger.warning(f"Could not read UniProt RELEASE.metalink from '{dir_url}': {exc}")
        return None


def _extract_protein_name(description: str) -> Optional[str]:
    match = _RECNAME_RE.search(description) or _SUBMITTED_NAME_RE.search(description)
    return match.group(1).strip() if match else None


def _extract_organism_scientific_name(organism: str) -> Optional[str]:
    match = _ORGANISM_RE.match(organism or "")
    return match.group(1).strip() if match else (organism or None)


def _entry_type(data_class: Optional[str]) -> str:
    # Literal strings match the live REST API's own `entryType` field
    # (confirmed live 2026-08-08), since `pipeline/uniprot/utils.py
    # ::_entry_type_is_reviewed` substring-matches against it.
    return "UniProtKB reviewed (Swiss-Prot)" if data_class == "Reviewed" else "UniProtKB unreviewed (TrEMBL)"


def _comments_to_json(comments: List[str]) -> List[Dict[str, Any]]:
    """
    `record.comments` is a flat list of strings like "FUNCTION: <text>"
    or "DISEASE: <name> [MIM:...]: <description>" -- Bio.SwissProt
    already strips the leading topic keyword off into no structure at
    all, so this re-splits on the first ": " to recover it. Only
    FUNCTION and DISEASE are converted -- the only two comment types
    `pipeline/uniprot/utils.py::parse_uniprot_entry` reads; every other
    comment type (SUBCELLULAR LOCATION, INTERACTION, SUBUNIT, ...) is
    dropped rather than carried through unused, keeping the cached
    dataset smaller than a byte-for-byte mirror of the live API would
    require.
    """
    result: List[Dict[str, Any]] = []
    function_captured = False
    for comment in comments:
        if ": " not in comment:
            continue
        topic, rest = comment.split(": ", 1)
        if topic == "FUNCTION" and not function_captured:
            result.append({"commentType": "FUNCTION", "texts": [{"value": rest.strip()}]})
            function_captured = True
        elif topic == "DISEASE":
            if ": " in rest:
                disease_id, description = rest.split(": ", 1)
            else:
                disease_id, description = rest, ""
            result.append(
                {
                    "commentType": "DISEASE",
                    "disease": {"diseaseId": disease_id.strip(), "description": description.strip()},
                }
            )
    return result


def _position_value(position: Any) -> Optional[int]:
    """
    Plain integer value of a `Bio.SeqFeature` position, or `None` when
    the position is a genuine `Bio.SeqFeature.UnknownPosition` --
    Swiss-Prot's own `?` marker for a feature boundary that is unclear
    in the source record (e.g. an uncertain signal-peptide cleavage
    site), confirmed real via `Bio.SeqFeature.Position.fromstring`
    (`text == "?"` -> `UnknownPosition()`), not a parse failure.

    Every other `Position` subclass Biopython produces from a real
    flat-file record (`ExactPosition`, `BeforePosition`,
    `AfterPosition`, `WithinPosition`, `OneOfPosition`, ...) *is* an
    `int` subclass and converts normally; `UnknownPosition` is the one
    documented exception. Checking `isinstance(position, int)` (accept)
    rather than enumerating the non-int exception is deliberate: it
    stays correct even if Biopython ever adds another non-int Position
    type.

    Returning `None` here -- rather than raising or guessing a
    concrete residue number -- matches the read side's own tolerance
    for a missing position: `pipeline/uniprot/utils.py::_extract_features`
    already reads `location.get("start", {}).get("value")` with plain
    `.get()`, and `UniProtFeature.overlaps` already treats a `None`
    begin/end as "no match" rather than a guess. This is the same
    never-guess convention PM1/PVS1/gene-resolution already use
    elsewhere in this codebase for a genuinely unknown coordinate.
    """
    return int(position) if isinstance(position, int) else None


def _features_to_json(features: List[Any]) -> List[Dict[str, Any]]:
    """
    Each `Bio.SeqFeature`-like feature's `.location` is a 0-based,
    half-open Python range (`[start, end)`), matching Python-slice
    convention -- the REST API's `location.start.value`/`.end.value`
    are 1-based inclusive, so `start` is offset by +1 while `end` is
    used as-is (a 0-based half-open end already equals the 1-based
    inclusive end for the same span). Offsetting an unknown start by
    +1 would turn a real `None` into `1`, a fabricated position, so the
    +1 is only applied when `_position_value` actually resolved a real
    integer.

    A feature with an unresolvable start and/or end is still kept
    (with `None` for whichever side was unknown) rather than dropped --
    one unknown boundary must not silently discard the feature's type/
    description, and must never abort conversion of the whole protein
    record it belongs to (see `_record_to_entry_json`, which has no
    try/except around this call precisely because it must not raise).
    Only a feature with no `.location` at all (a distinct, pre-existing
    case -- not produced by any FT line this module has observed) is
    skipped outright, since there is nothing positional to report.
    """
    result: List[Dict[str, Any]] = []
    for feat in features:
        location = getattr(feat, "location", None)
        if location is None:
            continue
        start_value = _position_value(location.start)
        end_value = _position_value(location.end)
        result.append(
            {
                "type": getattr(feat, "type", None) or "Unknown",
                "description": (getattr(feat, "qualifiers", None) or {}).get("note"),
                "location": {
                    "start": {"value": start_value + 1 if start_value is not None else None},
                    "end": {"value": end_value},
                },
            }
        )
    return result


def _record_to_entry_json(record: "SwissProt.Record") -> Optional[Dict[str, Any]]:
    if not record.accessions:
        return None
    return {
        "entryType": _entry_type(record.data_class),
        "primaryAccession": record.accessions[0],
        "uniProtkbId": record.entry_name,
        "proteinDescription": {"recommendedName": {"fullName": {"value": _extract_protein_name(record.description)}}},
        "organism": {"scientificName": _extract_organism_scientific_name(record.organism)},
        "sequence": {"length": record.sequence_length},
        "comments": _comments_to_json(record.comments),
        "features": _features_to_json(record.features),
    }


def _gene_symbol_from_record(record: "SwissProt.Record") -> Optional[str]:
    if not record.gene_name:
        return None
    name = (record.gene_name[0] or {}).get("Name")
    return name.strip().upper() if name else None


def _download_and_convert(dat_url: str, dest_path: str, version: Optional[str]) -> bool:
    """
    Streams the gzip-compressed Swiss-Prot flat file to a temp file,
    parses it record-by-record via `Bio.SwissProt.parse` (never holding
    the full decompressed text in memory), converts each into the JSON
    entry shape `parse_uniprot_entry` reads, and writes the result as
    JSON-lines -- atomically (temp file + rename), same pattern as
    every other bootstrap module in this codebase. Never raises.

    When more than one record maps to the same gene symbol (isoform-
    style duplicate entries occasionally occur in the reference
    proteome), the first REVIEWED entry wins and is never overwritten
    by a later unreviewed one -- matching `CONFIG.uniprot.REVIEWED_ONLY`'s
    default preference for the live API path.
    """
    raw_path = dest_path + ".raw.dat.gz"
    try:
        with requests.get(dat_url, timeout=CONFIG.uniprot.AUTO_FETCH_TIMEOUT_SECS, stream=True) as response:
            response.raise_for_status()
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            with open(raw_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
    except (requests.RequestException, OSError) as exc:
        logger.warning(f"UniProt reference-proteome download from '{dat_url}' failed: {exc}")
        if os.path.exists(raw_path):
            os.remove(raw_path)
        return False

    entries: Dict[str, Dict[str, Any]] = {}
    reviewed_genes: set = set()
    try:
        with gzip.open(raw_path, "rt", encoding="utf-8", errors="replace") as fh:
            for record in SwissProt.parse(fh):
                gene = _gene_symbol_from_record(record)
                if not gene:
                    continue
                if gene in entries and record.data_class != "Reviewed" and gene in reviewed_genes:
                    continue  # a reviewed entry for this gene is already indexed -- don't downgrade it
                try:
                    entry = _record_to_entry_json(record)
                except Exception as exc:  # noqa: BLE001 - one malformed record must never abort the whole ~200k-record proteome parse (see the UnknownPosition incident this guards against)
                    logger.warning(
                        f"Could not convert UniProt record for gene '{gene}' "
                        f"({getattr(record, 'entry_name', '?')}): {exc}"
                    )
                    continue
                if entry is None:
                    continue
                entries[gene] = entry
                if record.data_class == "Reviewed":
                    reviewed_genes.add(gene)
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not parse UniProt reference-proteome file '{raw_path}': {exc}")
        os.remove(raw_path)
        return False

    os.remove(raw_path)

    if not entries:
        logger.warning(f"UniProt reference-proteome fetch from '{dat_url}' produced zero usable entries; not caching.")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path), prefix=".uniprot_fetch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            for gene, entry in sorted(entries.items()):
                fh.write(json.dumps({"gene_symbol": gene, "entry": entry}) + "\n")
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        logger.warning(f"Could not write UniProt local dataset cache to '{dest_path}': {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    write_dataset_provenance_sidecar(
        dest_path, dat_url, version=(f"UniProt {version}" if version else None), compute_hash_from_file=True
    )
    logger.info(f"Cached {len(entries)} UniProt gene entries ({version or 'version unknown'}) to '{dest_path}'.")
    return True


def ensure_dataset_file() -> Optional[str]:
    """Path to a local, gene-symbol-indexed JSON-lines UniProt dataset,
    fetching/refreshing it first if needed. None if unavailable."""
    if not CONFIG.uniprot.AUTO_FETCH_ENABLED or CONFIG.uniprot.OFFLINE_MODE:
        return None

    dest_path = dataset_cache_path()
    if _is_fresh(dest_path):
        return dest_path

    with _fetch_lock:
        if _is_fresh(dest_path):  # re-check inside the lock
            return dest_path
        dir_url = CONFIG.uniprot.AUTO_FETCH_DIR_URL
        dat_url = dir_url.rstrip("/") + "/" + _RAW_DOWNLOAD_FILENAME
        version = _discover_version(dir_url)
        logger.info(f"Fetching UniProt reference proteome from '{dat_url}' (cache miss or stale)...")
        if _download_and_convert(dat_url, dest_path, version):
            return dest_path

    # Fetch failed -- an existing stale copy is still better than
    # nothing (UniProt releases roughly every 8 weeks; a stale file is
    # far more useful than falling back to a live per-gene call for
    # every single lookup this run).
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.warning(f"Using stale cached UniProt dataset at '{dest_path}' after a failed refresh.")
        record_stale_fallback(source="UniProt", path=dest_path, reason="failed refresh")
        return dest_path
    return None
