"""
Data-source version pinning and run provenance.

Why this exists: ClinVar, ClinGen, gnomAD, HPO, Orphanet, InterPro, and the
PS3/BS3 functional-evidence sources all change on their own schedules
(ClinVar weekly) -- a report that says "ClinVar reports Pathogenic" with no
record of WHICH ClinVar snapshot cannot be reproduced later. This module is
the mechanism that makes a GEPER report reproducible: it records, for every
external data source consulted in a run, whatever version/release/hash/
timestamp identifier that source actually exposes -- and is honest when one
doesn't exist, rather than fabricating a uniform-looking answer.

Audit result (verified live against each source during development, not
assumed -- see `RunProvenanceCollector`'s own per-source capture call sites
in `pipeline/orchestrator.py` for where each of these was confirmed):

  Real, queryable version/release identifier:
    - gnomAD: the GraphQL `datasetId` ("gnomad_r4"/"gnomad_r2_1") IS the
      release identifier -- known statically per query, not fetched.
    - dbSNP: `last_update_build_id` in the variation-detail response
      (api.ncbi.nlm.nih.gov/variation/v0/refsnp/{id}) -- a real dbSNP
      build number (verified: 157).
    - InterPro: every live REST response carries an `InterPro-Version`
      header (verified: "109.0"), including on a single-protein query.
    - UniProt: every live REST response carries `X-UniProt-Release` /
      `X-UniProt-Release-Date` / `X-API-Deployment-Date` headers
      (verified: release "2026_02").
    - AlphaFold DB: already captured per-entry as `model_version`
      (`pipeline/alphafold/provider.py`, from the summary API's
      `latestVersion`) -- no change needed here, just surfaced.
    - Orphanet: already captured as `data_version`, parsed from the
      `date` attribute of the downloaded XML's own root `<JDBOR>`
      element (`pipeline/orphanet/utils.py::parse_gene_disorder_xml`)
      -- no change needed here either.
    - Ensembl: `/info/data` returns the current release number
      (verified: `{"releases": [116]}`) -- one lightweight call per
      run, not per-variant (the release cannot change mid-run).
    - ClinGen gene-validity download: the server's `Content-Disposition`
      filename embeds the release date (verified:
      "Clingen-Gene-Disease-Summary-2026-07-31.csv") in addition to
      `Last-Modified`.
    - MANE Select summary download: the release version is embedded in
      the downloaded file's own name on NCBI's directory listing
      (verified live 2026-08-08: "MANE.GRCh38.v1.5.summary.txt.gz" ->
      "MANE 1.5"), parsed at bootstrap time -- see
      `pipeline/mane/bootstrap.py`.
    - BLAST (local mode): `database/blast_client.py::get_blast_tool_versions`
      already captures each local blast+ CLI tool's own `-version`
      output -- no change needed here, just surfaced.
    - AlphaMissense catalogue (GCS-hosted): the download response's
      `ETag` (GCS's own MD5 of the object), `x-goog-hash`, and
      `x-goog-generation` headers are a precise, server-authoritative
      content identifier -- verified live via a HEAD request (no full
      ~9GB download performed; this module only ever inspects headers
      captured by the code path that already downloads this file for
      real use).

  No release version, but a content hash and/or download timestamp is
  obtainable (the cached/bootstrapped file itself, once fetched):
    - ClinGen dosage-sensitivity download: `Last-Modified`/`ETag`
      headers exist but no embedded release date; sha256 of the
      downloaded content is computed and persisted instead.
    - HPO genes_to_phenotype.txt: same -- `Last-Modified`/`ETag`
      headers exist, no in-content version; sha256 persisted.

  Genuinely no version identifier available via the API GEPER uses --
  reported as UNKNOWN with an explanatory note, not silently omitted or
  guessed, per this module's core design principle (see `VersionStatus`):
    - ClinVar (E-utilities esummary): no database-wide release version
      is exposed by this API. Individual records already carry their
      own `last_evaluated` date (`database/clinvar_client.py`), which
      is NOT the same claim as "this is ClinVar release X" -- reported
      honestly as source-level UNKNOWN, with the per-record dates still
      visible in each variant's own ClinVar evidence.
    - ClinGen Evidence Repository (ERepo) / MaveDB (PS3/BS3 functional
      evidence): no source-wide API version; individual records/score-
      sets carry their own `publishedDate`, same treatment as ClinVar
      above.
    - MyVariant.info (GERP++ fallback): the only version-shaped field on
      a record is `_version`, which is MyVariant.info's internal
      Elasticsearch document-revision counter, NOT a dbNSFP/GERP data
      release -- verified live and deliberately NOT used here, since
      presenting it as a data version would be actively misleading.
    - BLAST remote mode (NCBI QBlast): the `nt` database updates
      continuously with no version exposed via the search API.
    - UCSC conservation API (PhyloP/PhastCons): DOES expose a genuine
      per-track `dataTime` field (verified live) -- NOT wired through to
      `ConservationAnnotation` in this pass (that dataclass is shared
      across the bigWig/API/MyVariant sources and threading a new field
      through all three call sites is a larger change than this pass's
      scope) -- disclosed here as a known gap, not silently dropped;
      only the query timestamp is captured for this source today.

Design principle (point 6 of the task this module was built for): "version
unknown" must be structurally distinguishable from "source not consulted".
`RunProvenanceCollector` is pre-seeded with every known source name at
construction time, each starting at `VersionStatus.NOT_CONSULTED` -- a
source stays in that state for the whole run if (and only if) nothing in
the pipeline ever actually queried it (e.g. ClinGen disabled via config).
`VersionStatus.UNKNOWN` is a DIFFERENT, later state a source can only reach
by first being consulted (see the priority ordering in `RunProvenanceCollector
.record`) -- so the two can never collapse into each other the way an absent
dict key collapsed "never ran" and "ran, found nothing" before the schema
work (`pipeline/stage_schemas.py`).
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Per-source provenance record
# ---------------------------------------------------------------------------


class VersionStatus(str, enum.Enum):
    """
    `NOT_CONSULTED` and `UNKNOWN` are deliberately two different states,
    not one -- see this module's own docstring for why collapsing them
    would repeat the exact bug class `pipeline/stage_schemas.py` was
    built to fix. Ordered here from least to most informative; this
    ordering IS the priority `RunProvenanceCollector.record` uses to
    decide whether a new record may overwrite an existing one (a
    later, degraded query must never downgrade an already-captured
    real version).
    """

    NOT_CONSULTED = "not_consulted"  # never queried this run
    UNKNOWN = "unknown"  # queried, but no version/hash/anything beyond a timestamp was obtainable
    TIMESTAMP_ONLY = "timestamp_only"  # queried; only a query/download time is meaningful (kept distinct from UNKNOWN: this is the honest floor every consulted source gets)
    HASH_ONLY = "hash_only"  # no release version, but a content hash of the actual bytes used is known
    VERSION_KNOWN = "version_known"  # a real, source-published version/release/build identifier is known

    def __bool__(self):
        raise TypeError(
            "VersionStatus has no truth value -- every member of a `str` enum is truthy, "
            "so `if x:`/`if not x:` silently collapses NOT_CONSULTED (never queried) and UNKNOWN (queried, nothing obtainable) "
            "into one answer, which is exactly the distinction this type exists to "
            "keep. Compare explicitly, e.g. `x is VersionStatus.VERSION_KNOWN`."
        )


_STATUS_PRIORITY = {
    VersionStatus.NOT_CONSULTED: 0,
    VersionStatus.UNKNOWN: 1,
    VersionStatus.TIMESTAMP_ONLY: 2,
    VersionStatus.HASH_ONLY: 3,
    VersionStatus.VERSION_KNOWN: 4,
}


class RetrievalMode(str, enum.Enum):
    """
    WHERE this run's answer from a source actually came from -- a
    SEPARATE AXIS from `VersionStatus`, which is only ever about
    version identifiability.

    Deliberately NOT a sixth `VersionStatus` member, for two reasons:

      1. The two facts are independent. A BLAST result replayed from
         the on-disk cache in local mode still has a real, known
         blast+ tool version; a live remote query has none. Every
         combination of (version identifiability x retrieval mode) is
         reachable, so folding one into the other would force
         recording either fact to destroy the other -- the same
         collapse `VersionStatus`'s own docstring exists to prevent.
      2. `VersionStatus` is TOTALLY ORDERED, and that ordering is
         load-bearing: `RunProvenanceCollector.record` uses it to stop
         a later degraded query downgrading an already-captured real
         version. "Replayed from cache" is not more or less
         informative than "version known" -- there is no honest place
         to put it in that ordering. Retrieval mode therefore MERGES
         (see `RunProvenanceCollector.note_retrieval`) rather than
         ranking.

    Why it matters clinically: a cache replay returns bytes a previous
    run obtained, and this run cannot verify they are still current.
    ClinVar reclassifies weekly; NCBI's `nt` changes continuously. A
    report that cannot distinguish "we asked the source and it said
    this" from "we reused what it said some unrecorded time ago" is
    making a stronger claim than it can support.
    """

    NOT_RETRIEVED = "not_retrieved"  # tracked this run, but no answer was obtained from this source at all
    LIVE = "live"  # every answer used this run came from contacting the source this run
    CACHE_REPLAY = "cache_replay"  # every answer used this run was replayed from a local cache written by an EARLIER run; the source itself was not contacted, and this run cannot verify the answer is still current
    MIXED = "mixed"  # both happened this run -- some answers live, some replayed (see note_retrieval: recording either alone would be a false claim about the other)

    def __bool__(self):
        raise TypeError(
            "RetrievalMode has no truth value -- same reason as VersionStatus: every member of a "
            "`str` enum is truthy, so `if x:` would silently collapse NOT_RETRIEVED (tried nothing) "
            "and CACHE_REPLAY (answered without contacting the source) into one answer. Compare "
            "explicitly, e.g. `x is RetrievalMode.CACHE_REPLAY`."
        )

    @classmethod
    def from_counts(cls, live: int, cache_replay: int) -> "RetrievalMode":
        """
        Maps "how many answers came from where" onto the honest single
        label for a run.

        Lives here, next to the vocabulary it produces, rather than in
        the orchestrator that happens to call it: it IS the meaning of
        these members, and putting it beside them is what lets the
        rule be tested without constructing a pipeline. Any future
        source that grows the same live/cached split gets one call, not
        a second copy of this reasoning.

        Note `live and cache_replay -> MIXED` comes FIRST. Checking
        either count alone first would silently describe a mostly-cached
        run as live (or the reverse), which is the whole failure being
        fixed.
        """
        if live and cache_replay:
            return cls.MIXED
        if live:
            return cls.LIVE
        if cache_replay:
            return cls.CACHE_REPLAY
        # Nothing was retrieved at all. Recorded explicitly rather than
        # left unset, so "this source returned nothing this run" stays
        # distinguishable from "nobody tracked this source".
        return cls.NOT_RETRIEVED


# Human-readable labels for `RetrievalMode`, defined HERE rather than in
# each renderer. `report/summary.py::_PROVENANCE_STATUS_LABELS` and
# `report/report_generator.py::_render_provenance`'s `status_labels`
# are two hand-maintained copies of the same map that only a comment
# keeps in step; this axis is not going to repeat that. Both renderers
# import this one.
RETRIEVAL_MODE_LABELS: Dict[str, str] = {
    RetrievalMode.NOT_RETRIEVED.value: "no data retrieved from this source this run",
    RetrievalMode.LIVE.value: "retrieved live from the source this run",
    RetrievalMode.CACHE_REPLAY.value: (
        "REPLAYED FROM LOCAL CACHE -- the source itself was not contacted this run, "
        "so this report cannot confirm the data is still current"
    ),
    RetrievalMode.MIXED.value: (
        "MIXED -- some data retrieved live this run, some replayed from local cache; "
        "the cached portion cannot be confirmed current"
    ),
}


class DataSourceProvenance(BaseModel):
    """
    One external data source's provenance for this run. `status` is
    REQUIRED (no default) -- matching `pipeline/stage_schemas.py
    ::StageEvidence`'s own pattern, a record can never be constructed
    without an explicit, honest status.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    status: VersionStatus
    # `None` means "retrieval mode was not tracked for this source",
    # which is NOT the same claim as RetrievalMode.NOT_RETRIEVED
    # ("tracked, and nothing was retrieved") -- and neither is a claim
    # that the data came live off the wire. Only BLAST is wired to
    # report retrieval mode today, so every other source stays None and
    # the renderers print nothing for it, rather than every unwired
    # source silently acquiring a "retrieved live" claim nobody
    # verified. Unlike `status` this therefore CAN default: the default
    # asserts nothing.
    retrieval: Optional[RetrievalMode] = None
    version: Optional[str] = None  # e.g. "gnomad_r4", "InterPro 109.0", "dbSNP build 157", "UniProt 2026_02"
    release_date: Optional[str] = (
        None  # ISO date/string if the source publishes one (Orphanet's `date`, ClinGen's filename date, UniProt's release date, ...)
    )
    content_hash: Optional[str] = (
        None  # sha256 (or a server-provided hash, e.g. GCS's ETag) of the actual bytes used, when applicable
    )
    hash_algorithm: Optional[str] = None  # "sha256" | "gcs-etag-md5" | ... -- which of the above `content_hash` is
    query_timestamp: Optional[str] = None  # UTC ISO -- when GEPER consulted this source THIS run (first successful use)
    endpoint: Optional[str] = None  # URL/endpoint queried or downloaded
    notes: Optional[str] = (
        None  # required explanation whenever status is UNKNOWN/TIMESTAMP_ONLY -- see `RunProvenanceCollector.record`
    )


# ---------------------------------------------------------------------------
# Content hashing + bootstrap-dataset sidecar metadata
# ---------------------------------------------------------------------------


def compute_file_sha256(path: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Streamed sha256 of a file on disk -- safe for large files (reads in
    8MB chunks, never loads the whole file into memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_path(dest_path: str) -> str:
    return dest_path + ".provenance.json"


def write_dataset_provenance_sidecar(
    dest_path: str,
    url: str,
    *,
    response_headers: Optional[Dict[str, str]] = None,
    release_date: Optional[str] = None,
    version: Optional[str] = None,
    content_hash: Optional[str] = None,
    hash_algorithm: Optional[str] = None,
    compute_hash_from_file: bool = True,
) -> Dict[str, Any]:
    """
    Persist a small `<dest_path>.provenance.json` sidecar recording what
    was actually downloaded and when, alongside a bootstrapped dataset
    cache file (ClinGen gene-validity/dosage, HPO genes_to_phenotype,
    Orphanet en_product6.xml, the AlphaMissense catalogue). Survives
    process restarts -- read back by `read_dataset_provenance_sidecar`
    at provenance-collection time regardless of whether THIS run
    triggered a fresh download or reused an already-cached file, which
    is exactly what reproducibility needs: "which file was actually
    used this run", independent of when it happened to be fetched.

    `compute_hash_from_file`: when `content_hash` isn't already known
    (e.g. from a server-provided hash header), sha256's `dest_path`
    directly -- safe for the ClinGen/HPO/Orphanet files (a few MB to
    ~20MB) but deliberately skippable for the ~9GB AlphaMissense
    catalogue, whose caller instead passes the GCS-provided `ETag`/
    `x-goog-hash` as `content_hash` with `hash_algorithm="gcs-etag-md5"`
    and `compute_hash_from_file=False` -- hashing 9GB locally on every
    (re)download would be its own real cost this module must not add.

    Never raises: a failed sidecar write is logged and the caller's own
    download/cache-refresh still succeeds -- provenance is additive
    metadata, never a precondition for a dataset actually being usable.
    """
    headers = response_headers or {}
    record: Dict[str, Any] = {
        "url": url,
        "downloaded_at": _utc_now_iso(),
        "http_last_modified": headers.get("Last-Modified"),
        "http_etag": headers.get("ETag"),
        "content_length": headers.get("Content-Length"),
        "release_date": release_date,
        "version": version,
        "content_hash": content_hash,
        "hash_algorithm": hash_algorithm,
    }
    if record["content_hash"] is None and compute_hash_from_file:
        try:
            record["content_hash"] = compute_file_sha256(dest_path)
            record["hash_algorithm"] = "sha256"
        except OSError as exc:
            logger.warning(f"Could not hash '{dest_path}' for its provenance sidecar: {exc}")

    try:
        sidecar = _sidecar_path(dest_path)
        tmp = sidecar + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, sidecar)
    except OSError as exc:
        logger.warning(f"Could not write provenance sidecar for '{dest_path}': {exc}")

    return record


def read_dataset_provenance_sidecar(dest_path: str) -> Optional[Dict[str, Any]]:
    """The persisted metadata for a bootstrapped dataset file, or None if
    it was never written (e.g. a manually-provisioned local file with no
    GEPER-tracked download) or is unreadable. Never raises."""
    sidecar = _sidecar_path(dest_path)
    if not os.path.exists(sidecar):
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read provenance sidecar '{sidecar}': {exc}")
        return None
    if not isinstance(data, dict):
        # `json.load` is typed `Any` -- a corrupted/hand-edited sidecar
        # could in principle contain a JSON list/string/number instead
        # of an object. Treated the same as "unreadable" (G1, report
        # review round 5: mypy flagged this function silently returning
        # `Any` where `Optional[Dict[str, Any]]` was declared) rather
        # than trusting the on-disk shape blindly.
        logger.warning(f"Provenance sidecar '{sidecar}' did not contain a JSON object; ignoring it.")
        return None
    return data


def local_file_provenance(source: str, path: Optional[str], *, endpoint: Optional[str] = None) -> DataSourceProvenance:
    """
    Provenance for a deployer-provisioned local file with NO GEPER-
    tracked download (e.g. `GEPER_CLINGEN_GENE_VALIDITY_FILE` pointed
    at a manually-placed copy) -- no sidecar can exist for a file GEPER
    never fetched itself, so this hashes the file directly (same
    reasoning as `write_dataset_provenance_sidecar`'s ClinGen/HPO/
    Orphanet case: these are small-to-modest files, not the AlphaMissense
    catalogue). `HASH_ONLY`, never `VERSION_KNOWN` -- GEPER has no way
    to know what release a manually-provided file actually is.
    """
    if not path or not os.path.exists(path):
        return DataSourceProvenance(
            source=source,
            status=VersionStatus.UNKNOWN,
            endpoint=endpoint,
            query_timestamp=_utc_now_iso(),
            notes="Configured local file path does not exist; cannot hash or version it.",
        )
    try:
        content_hash = compute_file_sha256(path)
    except OSError as exc:
        return DataSourceProvenance(
            source=source,
            status=VersionStatus.UNKNOWN,
            endpoint=path,
            query_timestamp=_utc_now_iso(),
            notes=f"Could not hash local file: {exc}",
        )
    return DataSourceProvenance(
        source=source,
        status=VersionStatus.HASH_ONLY,
        endpoint=path,
        content_hash=content_hash,
        hash_algorithm="sha256",
        query_timestamp=_utc_now_iso(),
        notes="Deployer-provisioned local file with no tracked download; no release version is known to GEPER, only its content hash.",
    )


# ---------------------------------------------------------------------------
# GEPER code version + AI model checkpoint identifiers
# ---------------------------------------------------------------------------


def get_geper_code_version() -> str:
    """
    The exact GEPER commit this run executed under, for reproducibility
    of the CODE, not just the data. Falls back honestly (never
    fabricates a version string) when this isn't a git checkout, git
    itself isn't installed, or the working tree has uncommitted changes
    (flagged with a "+dirty" suffix -- a report generated from a dirty
    tree is real and reproducible-in-principle, but not from the commit
    alone, and that distinction matters for the same reason this whole
    module exists).
    """
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if sha.returncode != 0:
            return "unknown (not a git checkout, or git unavailable)"
        commit = sha.stdout.strip()

        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            return f"{commit}+dirty"
        return commit
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown (git invocation failed: {exc})"


def get_runtime_environment_provenance() -> Dict[str, Any]:
    """
    OS, hardware, and thread-count facts for this run.

    [[provenance-captures-no-os-hardware-or-thread-count]]: `get_geper_code_version`
    (above) and `RunProvenanceCollector` capture the CODE and DATA halves of
    reproducibility, but nothing previously captured the HARDWARE half. Two
    runs with identical code_version and identical data-source versions can
    still have executed on different machines with different core/thread
    counts -- both legitimate sources of numerical non-determinism in CPU
    model inference (see REPRODUCIBILITY_PROTOCOL.md S2a) -- and without this,
    the provenance record could not distinguish "reproduced on the same
    machine" from "reproduced on a different one".

    Every field is captured honestly, matching this module's own convention
    elsewhere (`get_geper_code_version`): stdlib-only and best-effort, never
    raises, and a field this process cannot determine is reported as `None`
    with the reason, not silently omitted or guessed.
    """
    import platform

    result: Dict[str, Any] = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "torch_thread_count": None,
        "torch_thread_count_unavailable_reason": None,
    }

    # Best-effort: torch's own default CPU thread count is itself a source
    # of the reduction-order non-determinism this record exists to let a
    # later reproducibility comparison attribute correctly. Only meaningful
    # if torch is actually importable in this process -- absence is
    # reported honestly, not treated as an error.
    try:
        import torch

        result["torch_thread_count"] = torch.get_num_threads()
    except Exception as exc:  # noqa: BLE001 -- best-effort provenance capture, never fatal.
        result["torch_thread_count_unavailable_reason"] = str(exc)

    return result


def get_model_checkpoint_identifiers() -> Dict[str, str]:
    """
    The AI model checkpoint identifiers GEPER's own config already
    tracks (`config.py::ModelConfig` plus the splicing-plugin config
    flags) -- purely reads existing config, invokes no model loading or
    network I/O, safe to call unconditionally at pipeline startup.
    Only lists a model when it's actually configured/enabled, so this
    doubles as a record of which models this run's provenance covers.
    """
    mc = CONFIG.models
    identifiers: Dict[str, str] = {
        "hyenadna_checkpoint_dir": mc.HYENADNA_CHECKPOINT_DIR,
        "hyenadna_model_name": mc.HYENADNA_MODEL_NAME,
        "rna_fm": mc.RNA_FM,
        "esm2": mc.ESM2,
        "evo2_variant": mc.EVO2_VARIANT,
    }
    if CONFIG.mmsplice.ENABLED:
        identifiers["mmsplice"] = "mmsplice==2.4.0 (pinned, see requirements.txt)"
    if CONFIG.alphamissense.ENABLED:
        identifiers["alphamissense_catalogue_source"] = (
            "see 'AlphaMissense catalogue' in the data-source provenance list, not a model checkpoint"
        )
    if getattr(CONFIG.splicing, "ENABLE_SPLICEFORMER", False):
        identifiers["spliceformer"] = "spliceformer (see pipeline/models/spliceformer_plugin.py for checkpoint URL)"
    if getattr(CONFIG.splicing, "ENABLE_SPLICEBERT", False):
        identifiers["splicebert"] = "splicebert (HuggingFace BertForMaskedLM, see pipeline/models/splicebert_plugin.py)"
    if getattr(CONFIG.splicing, "ENABLE_ENFORMER", False):
        identifiers["enformer"] = "enformer-pytorch (see pipeline/models/ensemble.py)"
    if getattr(CONFIG.splicing, "ENABLE_BORZOI", False):
        identifiers["borzoi"] = "borzoi-pytorch (see pipeline/models/ensemble.py)"
    return identifiers


# Maps each key `get_model_checkpoint_identifiers()` uses onto the
# model key `pipeline/models/status.py::DISPLAY_NAMES`/
# `build_ai_model_status()` tracks per variant -- the two dicts were
# built independently (one config-shaped, one per-variant-result-
# shaped) and don't share key names 1:1 (HyenaDNA is split into two
# checkpoint-identifier keys here; AlphaMissense's identifier key names
# the catalogue, not the model). Not in this map == no run-level
# status tracking exists for that identifier (currently none; kept as
# a map, not an assumption of 1:1 naming, so a future identifier key
# doesn't silently get mis-attributed to the wrong model's status).
_CHECKPOINT_NAME_TO_MODEL_KEY: Dict[str, str] = {
    "hyenadna_checkpoint_dir": "hyenadna",
    "hyenadna_model_name": "hyenadna",
    "evo2_variant": "evo2",
    "rna_fm": "rna_fm",
    "esm2": "esm2",
    "mmsplice": "mmsplice",
    "alphamissense_catalogue_source": "alphamissense",
    "spliceformer": "spliceformer",
    "splicebert": "splicebert",
    "enformer": "enformer",
    "borzoi": "borzoi",
}


def finalize_model_checkpoint_provenance(
    identifiers: Dict[str, str], run_status: Dict[str, Dict[str, str]]
) -> Dict[str, Dict[str, str]]:
    """
    Enriches `get_model_checkpoint_identifiers()`'s flat identifier
    strings with each model's actual run-level status (see
    `pipeline.models.status.rollup_run_status`) -- reusing that
    module's existing USED/SKIPPED/DISABLED/FAILED vocabulary rather
    than inventing a parallel one, the same way `VersionStatus` already
    distinguishes "not consulted" from "consulted" for external data
    sources (D1, report review round 3). Called once, after every
    variant has been processed (see `pipeline/orchestrator.py::run()`)
    -- calling it at startup, before any variant ran, would repeat the
    exact timing bug D1 fixed for the bootstrapped datasets, since
    whether a model actually loaded/ran is only known after the fact.

    A checkpoint identifier with no run-level status tracked for it
    (not in `_CHECKPOINT_NAME_TO_MODEL_KEY`, or `run_status` doesn't
    mention that model key -- e.g. a run with zero variants) keeps its
    plain identifier string unchanged rather than fabricating a status.
    """
    enriched: Dict[str, Any] = {}
    for name, identifier in identifiers.items():
        model_key = _CHECKPOINT_NAME_TO_MODEL_KEY.get(name)
        status_entry = run_status.get(model_key) if model_key else None
        if status_entry is None:
            enriched[name] = identifier
        else:
            enriched[name] = {
                "identifier": identifier,
                "status": status_entry.get("status", "unknown"),
                "reason": status_entry.get("reason", ""),
            }
    return enriched


# ---------------------------------------------------------------------------
# Per-run collector
# ---------------------------------------------------------------------------

# Every source this collector knows how to seed as NOT_CONSULTED at
# construction time -- deliberately a fixed, named list (not derived
# from whatever happens to get `.record()`ed) so a source that is
# disabled/never queried this run still appears in the provenance
# output as NOT_CONSULTED, rather than being silently absent -- the
# structural distinction point 6 of this module's task required.
KNOWN_SOURCES = (
    "ClinVar",
    "dbSNP",
    "gnomAD",
    "ClinGen (gene validity)",
    "ClinGen (dosage sensitivity)",
    "HPO",
    "Orphanet",
    "UniProt",
    "InterPro",
    "AlphaFold DB",
    "BLAST",
    "AlphaMissense catalogue",
    "Ensembl",
    # Distinct from plain "Ensembl" above (that entry is the REST API's
    # own `/info/data` release number, e.g. "Ensembl release 116",
    # captured once per run by `capture_ensembl_release`) -- this is the
    # separately bootstrapped/cached local GTF+CDS gene/transcript
    # dataset file `pipeline/ensembl/bootstrap.py` downloads, with its
    # own hash/timestamp sidecar. Previously not registered here at all,
    # so `RunProvenanceCollector.record` silently dropped every capture
    # for it with an "unrecognized source" warning nobody acted on --
    # exactly the missing-vs-empty pattern D1 fixed, just for a source
    # name instead of a status (report review round 4).
    "Ensembl (GTF+CDS gene/transcript cache)",
    "Conservation (PhyloP/PhastCons, UCSC)",
    "Conservation (GERP++, MyVariant.info)",
    "Functional evidence (ClinGen ERepo)",
    "Functional evidence (MaveDB)",
    "MANE Select (NCBI)",
)

# Evidence-source short names, exactly as recorded in a variant's
# `clinical_report["evidence_sources"]` (see
# `report/clinical_report_builder.py::_REFERENCES`), mapped onto the
# `KNOWN_SOURCES` prefix(es) they correspond to here -- a prefix match,
# not an exact one, since ClinGen and AlphaMissense each expand to a
# more specific provenance source name above (e.g.
# "ClinGen (gene validity)"). Sources with no provenance entry at all
# (MMSplice, protein_translator -- GEPER's own code, not an external
# data source) are simply absent from this mapping; there is no version
# to look up for them. Shared by `report/summary.py` (the one-page
# Clinician Summary's provenance-gap flag) and `compare_reports.py`
# (cross-run provenance diffing) so the two never define this mapping
# differently.
EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX: Dict[str, str] = {
    "ClinVar": "ClinVar",
    "dbSNP": "dbSNP",
    "gnomAD": "gnomAD",
    "ClinGen": "ClinGen",
    "UniProt": "UniProt",
    "InterPro": "InterPro",
    "AlphaFold DB": "AlphaFold DB",
    "AlphaMissense": "AlphaMissense",
    "BLAST": "BLAST",
}


class RunProvenanceCollector:
    """
    Accumulates one `DataSourceProvenance` per known source across an
    entire pipeline run. Owned by `GeperPipeline` (one instance per
    run, not per-variant) -- most of the version signals this module's
    docstring lists (gnomAD's dataset_id, InterPro/UniProt's response
    headers, Ensembl's release, bootstrapped-dataset hashes) are stable
    for a run's whole duration, so recording them once (the first
    successful capture) rather than per-variant is both correct and
    far cheaper.
    """

    def __init__(self) -> None:
        self._records: Dict[str, DataSourceProvenance] = {
            name: DataSourceProvenance(source=name, status=VersionStatus.NOT_CONSULTED) for name in KNOWN_SOURCES
        }

    def record(
        self,
        source: str,
        status: VersionStatus,
        *,
        version: Optional[str] = None,
        release_date: Optional[str] = None,
        content_hash: Optional[str] = None,
        hash_algorithm: Optional[str] = None,
        query_timestamp: Optional[str] = None,
        endpoint: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Records (or upgrades) `source`'s provenance. A new record only
        replaces the existing one when its status is at least as
        informative (`VersionStatus`'s own ordering) -- a later,
        degraded query (e.g. a transient failure on a second variant
        after a first variant's query already yielded a real version)
        must never downgrade an already-known-good record. `source`
        must be one of `KNOWN_SOURCES`; an unrecognized name is logged
        and ignored rather than silently expanding the provenance list
        with an ad hoc, uncatalogued entry.
        """
        if source not in KNOWN_SOURCES:
            logger.warning(
                f"RunProvenanceCollector.record: unrecognized source '{source}' -- not in KNOWN_SOURCES, ignoring."
            )
            return
        existing = self._records[source]
        if _STATUS_PRIORITY[status] < _STATUS_PRIORITY[existing.status]:
            return
        self._records[source] = DataSourceProvenance(
            source=source,
            status=status,
            # Carried forward, never reset: `record` is about the
            # VERSION axis and has no business clearing the RETRIEVAL
            # axis. The two capture paths run at different points in a
            # run (status at startup, retrieval after the variant
            # loop), and the orchestrator's post-loop
            # `_capture_bootstrapped_datasets_provenance` re-sweep can
            # re-`record` a source after its retrieval mode is already
            # known -- so a rebuild that dropped this field would erase
            # the cache-replay finding in the ordinary case, not an
            # exotic one.
            retrieval=existing.retrieval,
            version=version,
            release_date=release_date,
            content_hash=content_hash,
            hash_algorithm=hash_algorithm,
            query_timestamp=query_timestamp or _utc_now_iso(),
            endpoint=endpoint,
            notes=notes,
        )

    def note_retrieval(self, source: str, mode: RetrievalMode) -> None:
        """
        Records that `source` answered this run via `mode`, MERGING with
        whatever is already recorded rather than ranking (see
        `RetrievalMode`: there is no honest ordering between "live" and
        "replayed from cache", so `record`'s priority-overwrite rule
        would be the wrong instrument here).

        The merge rule that matters: LIVE and CACHE_REPLAY together
        become MIXED. A run that made nine cache replays and one live
        query is not honestly describable as either -- calling it LIVE
        implies nine unverifiable answers were fresh, calling it
        CACHE_REPLAY implies a live query never happened. MIXED is the
        only claim supported by what actually occurred.

        NOT_RETRIEVED is the identity element: it never overwrites a
        real retrieval, and any real retrieval overwrites it.
        """
        if source not in KNOWN_SOURCES:
            logger.warning(
                f"RunProvenanceCollector.note_retrieval: unrecognized source '{source}' -- "
                "not in KNOWN_SOURCES, ignoring."
            )
            return
        existing = self._records[source].retrieval
        if existing is None or existing is RetrievalMode.NOT_RETRIEVED:
            merged = mode
        elif mode is RetrievalMode.NOT_RETRIEVED or mode is existing:
            merged = existing
        else:
            # existing and mode are two different real retrieval modes
            # (LIVE vs CACHE_REPLAY, or either against an already-MIXED
            # record) -- the only honest answer is MIXED.
            merged = RetrievalMode.MIXED
        self._records[source] = self._records[source].model_copy(update={"retrieval": merged})

    def get(self, source: str) -> Optional[DataSourceProvenance]:
        return self._records.get(source)

    def to_list(self) -> List[Dict[str, Any]]:
        """Stable, alphabetical-by-source order -- deterministic output
        regardless of which order variants happened to trigger captures in."""
        return [self._records[name].model_dump(mode="json") for name in sorted(self._records)]


# ---------------------------------------------------------------------------
# Run-level capture helpers (called once per run, not per-variant)
#
# Kept as standalone functions -- rather than inline in
# `pipeline/orchestrator.py::GeperPipeline.__init__` -- specifically so
# each is independently unit-testable with a mocked `requests`/subprocess
# call, without constructing a full `GeperPipeline` (which loads real
# model registries and is out of scope for a lightweight test). The
# orchestrator's own wiring is therefore just: call these, feed the
# result into `RunProvenanceCollector.record(...)`, and swallow any
# exception -- never let a provenance capture affect whether the
# pipeline actually starts.
# ---------------------------------------------------------------------------


def capture_ensembl_release(endpoint: str = "https://rest.ensembl.org", timeout_secs: float = 8.0) -> Dict[str, Any]:
    """
    Ensembl's REST API exposes its current release number via
    `/info/data` (verified live: `{"releases": [116]}`) -- one
    lightweight call, safe to make once per run (the release cannot
    change mid-run, and every transcript-structure/sequence-context
    lookup this pipeline makes already depends on this same API).

    Returns a plain dict (not a `DataSourceProvenance` -- callers
    combine this with an explicit `query_timestamp`/`status` via
    `RunProvenanceCollector.record`) with `releases` (the raw list) and
    `version` (a human-readable "Ensembl release N" string, or None on
    any failure). Never raises.
    """
    import requests

    try:
        response = requests.get(
            f"{endpoint}/info/data", params={"content-type": "application/json"}, timeout=timeout_secs
        )
        response.raise_for_status()
        payload = response.json()
        releases = payload.get("releases") or []
        version = f"Ensembl release {releases[0]}" if releases else None
        return {"version": version, "releases": releases, "endpoint": endpoint, "error": None}
    except Exception as exc:  # noqa: BLE001 -- a provenance capture must never break pipeline startup
        return {"version": None, "releases": [], "endpoint": endpoint, "error": str(exc)}


def capture_blast_local_tool_versions() -> Dict[str, Any]:
    """
    Wraps `database/blast_client.py::get_blast_tool_versions` (already
    existing, unchanged by this module) into the same
    version/error-or-not shape the other `capture_*` helpers here use,
    so orchestrator wiring is uniform. Returns `{"version": None, ...}`
    when no local BLAST+ tools are on PATH at all (e.g. remote-mode-only
    or AI-only deployments) -- a legitimate, non-error outcome, not a
    failure.
    """
    from database.blast_client import get_blast_tool_versions

    try:
        versions = get_blast_tool_versions()
        found = {
            tool: v for tool, v in versions.items() if v and not v.startswith("error") and "not found" not in (v or "")
        }
        if not found:
            return {"version": None, "tools": versions, "error": None}
        version = "; ".join(f"{tool} {v}" for tool, v in sorted(found.items()))
        return {"version": version, "tools": versions, "error": None}
    except Exception as exc:  # noqa: BLE001 -- a provenance capture must never break pipeline startup
        return {"version": None, "tools": {}, "error": str(exc)}
