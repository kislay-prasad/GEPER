"""
Packaging Part 3 mechanism -- the manifest and the verified update path.

`PACKAGING_OFFLINE_DATABASE_CACHES.md` found that a deployment cannot
today state which version of each bundled database it holds, and that
there is no checksum-verified way to move a new snapshot onto a site
with no internet access. This module is that mechanism:

  - `build_manifest_entry`/`build_manifest`: one entry per known cache
    source naming its VERSION, CHECKSUM, and DATE FETCHED -- built from
    whatever `pipeline/provenance.py` sidecar already exists on disk
    (or, for a deployer-provisioned `*_LOCAL_FILE`, hashed directly),
    the same signal `pipeline/orchestrator.py
    ::_capture_bootstrapped_dataset_provenance` already reads for a
    live run's own provenance section -- this just makes it queryable
    WITHOUT running a pipeline, which is what "a deployment must be
    able to state which version of each database it holds" requires.
  - `verify_manifest_entry`/`verify_manifest`: recomputes each file's
    checksum and reports a mismatch by source name -- the check a
    corrupted or silently-swapped cache file needs.
  - `apply_verified_update`: the only way this module offers to change
    a cached dataset file -- refuses (raises `ChecksumMismatchError`,
    leaves the existing file untouched) unless the incoming snapshot's
    own sha256 matches what the caller expected, THEN atomically
    replaces the file and records the update's own sidecar.

Deliberately NOT wired into `pipeline/orchestrator.py`: this is queried
independently of a pipeline run (a deployment operator asking "what do
I have" or "let me update X"), and out of scope per the dispatch is
anything about the build/bundling step itself (Dockerfile, image
contents, sizes) -- this module only ever reads/writes the same
on-disk cache paths every bootstrap module already manages.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pipeline.provenance import (
    _utc_now_iso,
    compute_file_sha256,
    read_dataset_provenance_sidecar,
    write_dataset_provenance_sidecar,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class ChecksumMismatchError(Exception):
    """Raised by `apply_verified_update` when an incoming snapshot's
    sha256 does not match what the caller expected. The existing cache
    file at `dest_path` is guaranteed untouched when this is raised --
    the checksum is verified BEFORE any write to `dest_path` happens."""


@dataclass(frozen=True)
class UpdateResult:
    source: str
    dest_path: str
    checksum: str
    accepted: bool
    applied_at: str


def build_manifest_entry(source: str, *, configured_local_file: str = "", auto_fetch_path: str = "") -> Dict[str, Any]:
    """
    One manifest row for `source`: whichever file it will actually be
    read from (an explicit deployer-provisioned local file takes
    precedence over the auto-fetch cache path -- same precedence as
    `pipeline/orchestrator.py::_capture_bootstrapped_dataset_provenance`),
    naming its version/checksum/date-fetched, or honestly `"absent"`
    with every value `None` rather than a plausible-looking guess when
    no file exists yet at either path.
    """
    path = configured_local_file or auto_fetch_path
    if not path or not os.path.exists(path):
        return {
            "source": source,
            "path": path or None,
            "status": "absent",
            "version": None,
            "checksum": None,
            "hash_algorithm": None,
            "date_fetched": None,
            "release_date": None,
        }

    sidecar = read_dataset_provenance_sidecar(path)
    if sidecar is not None:
        checksum = sidecar.get("content_hash")
        hash_algorithm = sidecar.get("hash_algorithm")
        date_fetched = sidecar.get("downloaded_at")
        version = sidecar.get("version")
        release_date = sidecar.get("release_date")
    else:
        # No sidecar (e.g. a deployer manually placed `configured_local_file`
        # with no GEPER-tracked download) -- hash the file directly, same
        # reasoning as `pipeline/provenance.py::local_file_provenance`.
        checksum = None
        hash_algorithm = None
        date_fetched = None
        version = None
        release_date = None

    if checksum is None:
        try:
            checksum = compute_file_sha256(path)
            hash_algorithm = "sha256"
        except OSError as exc:
            logger.warning(f"Could not hash '{path}' for the database manifest: {exc}")

    return {
        "source": source,
        "path": path,
        "status": "present",
        "version": version,
        "checksum": checksum,
        "hash_algorithm": hash_algorithm,
        "date_fetched": date_fetched,
        "release_date": release_date,
    }


def verify_manifest_entry(
    entry: Dict[str, Any], *, configured_local_file: str = "", auto_fetch_path: str = ""
) -> Optional[str]:
    """
    Recomputes `entry`'s file's checksum and compares it against the
    checksum the manifest recorded. Returns `None` when it matches (or
    when the entry never had a file to check, i.e. `status == "absent"`
    and the file is still absent), or a human-readable problem string
    naming the source when it does not -- corruption, tampering, or a
    file quietly replaced out from under the manifest all look
    identical here, which is the honest thing to report: this function
    can only say the bytes changed, not why.
    """
    path = configured_local_file or auto_fetch_path or entry.get("path")
    source = entry.get("source", "<unknown source>")

    if not path or not os.path.exists(path):
        if entry.get("status") == "absent":
            return None
        return f"{source}: manifest recorded a file at '{path}' but no file exists there now."

    if entry.get("status") == "absent":
        return f"{source}: manifest recorded no file, but a file now exists at '{path}' -- manifest is stale, regenerate it."

    expected = entry.get("checksum")
    if not expected:
        return None  # nothing to verify against (e.g. hashing failed when the manifest was built)

    actual = compute_file_sha256(path)
    if actual != expected:
        return (
            f"{source}: checksum mismatch for '{path}' -- manifest recorded '{expected}', "
            f"file now hashes to '{actual}'. Possible corruption or tampering; do not trust this file."
        )
    return None


def build_manifest() -> Dict[str, Any]:
    """
    The full manifest: one entry per known bootstrap-managed cache
    source, naming its version/checksum/date-fetched. This is what
    answers "which version of each database does this deployment
    hold" -- read directly off disk, no pipeline run required.
    """
    from config import CONFIG
    from pipeline.clingen import bootstrap as clingen_bootstrap
    from pipeline.ensembl import bootstrap as ensembl_bootstrap
    from pipeline.hpo import bootstrap as hpo_bootstrap
    from pipeline.hpo import ontology as hpo_ontology
    from pipeline.mane import bootstrap as mane_bootstrap
    from pipeline.orphanet import bootstrap as orphanet_bootstrap
    from pipeline.uniprot import bootstrap as uniprot_bootstrap

    sources = [
        build_manifest_entry(
            "ClinGen (gene validity)",
            configured_local_file=CONFIG.clingen.GENE_VALIDITY_LOCAL_FILE,
            auto_fetch_path=clingen_bootstrap.gene_validity_cache_path(),
        ),
        build_manifest_entry(
            "ClinGen (dosage sensitivity)",
            configured_local_file=CONFIG.clingen.DOSAGE_SENSITIVITY_LOCAL_FILE,
            auto_fetch_path=clingen_bootstrap.dosage_sensitivity_cache_path(),
        ),
        build_manifest_entry(
            "HPO (gene-to-phenotype)",
            configured_local_file=CONFIG.hpo.LOCAL_FILE,
            auto_fetch_path=hpo_bootstrap.genes_to_phenotype_cache_path(),
        ),
        build_manifest_entry("HPO (ontology)", auto_fetch_path=hpo_ontology.ontology_cache_path()),
        build_manifest_entry("Orphanet", auto_fetch_path=orphanet_bootstrap.gene_disorder_cache_path()),
        build_manifest_entry(
            "MANE Select (NCBI)",
            configured_local_file=CONFIG.mane.LOCAL_FILE,
            auto_fetch_path=mane_bootstrap.summary_cache_path(),
        ),
        build_manifest_entry(
            "UniProt",
            configured_local_file=CONFIG.uniprot.LOCAL_DATASET_FILE,
            auto_fetch_path=uniprot_bootstrap.dataset_cache_path(),
        ),
        build_manifest_entry(
            "Ensembl (GTF+CDS gene/transcript cache)",
            configured_local_file=CONFIG.ensembl.LOCAL_FILE,
            auto_fetch_path=ensembl_bootstrap.dataset_cache_path(),
        ),
    ]
    return {"generated_at": _utc_now_iso(), "sources": sources}


def write_manifest(output_path: str) -> Dict[str, Any]:
    """Builds and persists the manifest as JSON at `output_path`. Returns
    the manifest dict that was written."""
    import json

    manifest = build_manifest()
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, output_path)
    return manifest


def verify_manifest(manifest: Dict[str, Any]) -> List[str]:
    """Verifies every entry in `manifest` against what is actually on
    disk right now. Returns a list of problem strings, empty when every
    entry's file matches its recorded checksum."""
    problems = []
    for entry in manifest.get("sources", []):
        problem = verify_manifest_entry(entry)
        if problem:
            problems.append(problem)
    return problems


def apply_verified_update(
    *,
    source: str,
    new_file_path: str,
    expected_checksum: str,
    dest_path: str,
    version: Optional[str] = None,
    release_date: Optional[str] = None,
) -> UpdateResult:
    """
    The update path for an air-gapped site: a new snapshot at
    `new_file_path` is only ever applied to `dest_path` if its sha256
    matches `expected_checksum` (the checksum the deployer was given
    out-of-band, alongside the file, from whoever produced the
    snapshot). Checksummed at minimum, per the dispatch.

    Verifies BEFORE touching `dest_path` at all: on a mismatch, raises
    `ChecksumMismatchError` and `dest_path` is guaranteed untouched --
    a caller can retry with a corrected file/checksum, or investigate,
    without having already lost the working cache. On a match, the
    replace is atomic (`os.replace` within the same directory), and a
    provenance sidecar is written recording the verified checksum as
    this update's own content hash.
    """
    actual_checksum = compute_file_sha256(new_file_path)
    if actual_checksum != expected_checksum:
        raise ChecksumMismatchError(
            f"{source}: incoming snapshot at '{new_file_path}' hashes to '{actual_checksum}', "
            f"which does not match the expected checksum '{expected_checksum}'. Refusing to apply -- "
            f"'{dest_path}' left untouched. Possible corruption or tampering in transit."
        )

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    tmp_path = dest_path + ".tmp"
    shutil.copyfile(new_file_path, tmp_path)
    os.replace(tmp_path, dest_path)

    write_dataset_provenance_sidecar(
        dest_path,
        url=f"verified-manual-update:{source}",
        content_hash=actual_checksum,
        hash_algorithm="sha256",
        version=version,
        release_date=release_date,
        compute_hash_from_file=False,
    )
    logger.info(f"Applied verified update for '{source}' at '{dest_path}' (sha256 {actual_checksum}).")
    return UpdateResult(
        source=source, dest_path=dest_path, checksum=actual_checksum, accepted=True, applied_at=_utc_now_iso()
    )
