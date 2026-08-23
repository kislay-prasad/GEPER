"""
NCBI BLAST+ client.

Supports three modes:
  - "remote": uses Biopython's NCBIWWW.qblast to call NCBI's hosted
    BLAST+ web service. Requires no local BLAST+ installation, which
    keeps GEPER's Colab footprint minimal, but is by far the slowest
    external call in the whole pipeline -- NCBI's hosted queue
    typically takes anywhere from ~30s to several minutes per
    submission, and (unlike ClinVar/dbSNP/Ensembl) that time is spent
    sitting in NCBI's queue rather than doing meaningful local work,
    so it dominates end-to-end runtime on any VCF with more than a
    handful of variants.
  - "local": shells out to a local `blastn` binary (NCBI BLAST+
    command line tools) against a locally configured database, for
    deployments where low-latency, high-throughput BLAST is required
    and the tools/DB are provisioned on the server. Typically 100x+
    faster than "remote" since there's no network queue at all.
  - "auto" (default): prefers "local" automatically when a `blastn`
    binary AND a usable database are both present on this machine
    (either passed explicitly via `local_db_path`, or discovered via
    the `GEPER_BLAST_DATABASE` / `GEPER_BLAST_LOCAL_DB` / `BLASTDB`
    environment variables), and falls back to "remote" otherwise. This
    never requires the caller to know ahead of time whether the
    deployment has local BLAST+ provisioned -- it just uses the fast
    path when available. If neither a usable local database nor a
    working remote path (Biopython) is available, "auto" degrades one
    step further to a graceful no-op skip (see `_resolve_auto_mode`'s
    caller in `__init__` and the "skip" mode branches in `search()` /
    `search_many()`) rather than raising -- the rest of the pipeline
    keeps running without BLAST evidence for that instance.

Backend priority for "auto" (this is the whole point of this module):
  1. Local BLAST+ (blastn/makeblastdb/blastdbcmd) -- fastest, no
     network dependency once the database is provisioned.
  2. Remote NCBI BLAST -- functional fallback, no local install
     required, but far slower.
  3. Graceful skip -- if neither is usable, BLAST is skipped for this
     instance instead of failing the run.

Local BLAST+ database configuration:
  - `GEPER_BLAST_DATABASE` (or the `local_db_path` constructor arg) --
    path/prefix of a prebuilt BLAST database (the `-db` value blastn
    itself expects, e.g. `/data/blastdb/GRCh38`).
  - `GEPER_BLAST_MODE=auto|local|remote` -- selects the backend
    priority described above; defaults to "auto".
  - `GEPER_BLAST_REFERENCE_FASTA` (or the `reference_fasta` constructor
    arg) -- path to a FASTA reference. If set and no database is found
    at `GEPER_BLAST_DATABASE`, a database is automatically built there
    with `makeblastdb` the first time it's needed (never rebuilt if it
    already exists -- see `ensure_local_blast_db`).
  - The standard NCBI `BLASTDB` environment variable is still honored
    as a fallback for `GEPER_BLAST_DATABASE`/`GEPER_BLAST_LOCAL_DB`,
    for compatibility with existing NCBI BLAST+ deployments.

All three modes return an identically-shaped normalized dict (see
`_normalize_biopython_record` / `_search_local`) so callers (the
orchestrator) never need to know which mode produced a given result,
and so this performance work never changes what a BLAST result *is*
-- only how quickly / how many times it has to be computed.

Performance layers (Phase 3 performance pass), all strictly additive
and all safe to disable/fall back from:
  1. In-memory, per-instance memoization of `search()` by exact
     (mode, program, database, max_hits, sequence) -- unchanged from
     the original implementation.
  2. A persistent, cross-run, on-disk cache (SQLite; stdlib only, no
     new dependency) keyed the same way, so a sequence BLASTed in a
     previous run (or a previous checkpointed attempt of the same
     run) is never re-submitted to NCBI at all.
  3. `search_many()`: submits a batch of *distinct* sequences
     concurrently (ThreadPoolExecutor) instead of one at a time,
     since remote BLAST time is dominated by waiting in NCBI's queue,
     not local CPU -- concurrent requests overlap that wait instead
     of paying it serially, once per variant.
  4. `mode="auto"` local-database preference (see above).

None of these layers change a single BLAST hit, score, or e-value --
they only change how many network round trips / how much wall-clock
time it takes to obtain the exact same result. See
verify_blast_optimization.py for the correctness proof and
benchmark_blast.py for before/after timing.

"remote" mode depends on Biopython (listed in requirements.txt), which
-- like RNA-FM's `rna-fm` -- may not actually be installed in a
given Colab/runtime session even though it's declared as a dependency.
`ensure_pip_package_available` auto-installs it the first time it's
actually needed (see utils/auto_install.py), so BLAST works without a
manual `pip install biopython` step, the same way the model wrappers do.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from config import CONFIG
from utils.auto_install import PackageCheckStatus, check_pip_package_availability, ensure_pip_package_available
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


def _cache_key(mode_identity: str, program: str, database: str, max_hits: int, sequence: str) -> str:
    """
    Stable hash for a BLAST call, used both as the in-memory dict key
    and the on-disk cache row key. Hashing (rather than using the raw
    tuple, which can be megabytes for a long sequence) keeps disk-cache
    rows small and lookups fast regardless of sequence length.

    `mode_identity` distinguishes "remote" from "local", AND
    distinguishes different local databases from each other (two
    different local_db_path values must never share a cache entry --
    the same sequence can legitimately get different hits from
    different databases). See BLASTClient._mode_identity().
    """
    payload = f"{mode_identity}\x1f{program}\x1f{database}\x1f{max_hits}\x1f{sequence}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Local BLAST+ database detection / auto-build / tool verification
# ---------------------------------------------------------------------------
# These are module-level (not just methods on BLASTClient) so that
# verify_environment.py's startup validation can call them directly
# without constructing a full BLASTClient (which also touches Biopython
# auto-install, disk cache setup, etc. -- more than a read-only
# diagnostic script should trigger).

_LOCAL_BLAST_TOOLS = ("blastn", "makeblastdb", "blastdbcmd")


def _has_local_db_files(db_path: Optional[str]) -> bool:
    """
    True if `db_path` looks like an existing local BLAST database --
    i.e. at least one sibling file sharing that base name/prefix exists
    in its directory (a BLAST nucleotide database is a set of files
    such as `mydb.nin`/`.nsq`/`.nhr`, or `.ndb` for newer BLAST+
    versions, never one single exact file at `db_path`).

    Shared by `BLASTClient._resolve_auto_mode` and
    `ensure_local_blast_db` so "does a usable database already exist
    here" is answered identically (and only) in one place.
    """
    if not db_path:
        return False
    directory = os.path.dirname(db_path) or "."
    base = os.path.basename(db_path)
    try:
        return any(name.startswith(base) for name in os.listdir(directory))
    except OSError:
        return False


def get_blast_tool_versions() -> Dict[str, Optional[str]]:
    """
    Best-effort version string for each local BLAST+ command line tool
    (blastn, makeblastdb, blastdbcmd), or None per-tool if it isn't on
    PATH (or its `-version` invocation fails for any reason). Never
    raises -- this is read-only diagnostic information, used by
    verify_environment.py's startup validation report and safe to call
    from anywhere without side effects.
    """
    versions: Dict[str, Optional[str]] = {}
    for tool in _LOCAL_BLAST_TOOLS:
        binary = shutil.which(tool)
        if not binary:
            versions[tool] = None
            continue
        try:
            proc = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=10)
            first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
            versions[tool] = first_line[0] if first_line else "unknown version (empty -version output)"
        except Exception as exc:  # noqa: BLE001 - diagnostic only, must never raise
            versions[tool] = f"error invoking '-version': {exc}"
    return versions


def ensure_local_blast_db(
    fasta_path: Optional[str], db_path: Optional[str] = None, dbtype: str = "nucl"
) -> Optional[str]:
    """
    Ensure a local BLAST database exists for `fasta_path`, building one
    with `makeblastdb` if it doesn't already exist, and returning the
    resulting database path/prefix -- or None if no database could be
    obtained (missing FASTA, missing `makeblastdb`, or a build failure),
    in which case callers should fall back to remote BLAST rather than
    crash.

    Never rebuilds an existing database: if `_has_local_db_files`
    already finds database files at the target path, this is a
    no-op that just returns that path immediately.

    `db_path` defaults to `fasta_path` itself (makeblastdb writes its
    `.n*` index files alongside/next to `-out`, so building "in place"
    at the FASTA's own path is a reasonable default when no explicit
    database path is configured).
    """
    if not fasta_path:
        return None
    if not os.path.isfile(fasta_path):
        logger.warning(
            f"GEPER_BLAST_REFERENCE_FASTA / reference_fasta points to "
            f"'{fasta_path}', which does not exist or is not a file; "
            "cannot auto-build a local BLAST database from it."
        )
        return None

    resolved_db_path = db_path or fasta_path

    if _has_local_db_files(resolved_db_path):
        logger.info(
            f"Local BLAST database already exists at '{resolved_db_path}' -- skipping makeblastdb (not rebuilding)."
        )
        return resolved_db_path

    if shutil.which("makeblastdb") is None:
        logger.warning(
            f"A FASTA reference was provided ('{fasta_path}') but 'makeblastdb' "
            "is not on PATH, so no local BLAST database could be built "
            "automatically. Install NCBI BLAST+ command line tools (see "
            "verify_environment.py), or provide a prebuilt database via "
            "GEPER_BLAST_DATABASE instead."
        )
        return None

    command = [
        "makeblastdb",
        "-in",
        fasta_path,
        "-dbtype",
        dbtype,
        "-out",
        resolved_db_path,
        "-parse_seqids",
    ]
    logger.info(
        f"No local BLAST database found at '{resolved_db_path}'; building one "
        f"from '{fasta_path}' via makeblastdb (one-time; reused on every "
        "subsequent run)..."
    )
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CONFIG.api.REQUEST_TIMEOUT_SECS * 20,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            f"makeblastdb failed while building a database from '{fasta_path}' "
            f"({exc.stderr.strip() if exc.stderr else exc}); falling back "
            "without an auto-built local database."
        )
        return None
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            f"makeblastdb timed out while building a database from "
            f"'{fasta_path}' ({exc}); falling back without an auto-built "
            "local database."
        )
        return None
    except Exception as exc:  # noqa: BLE001 - must never crash the pipeline
        logger.warning(
            f"Unexpected error running makeblastdb ({exc}); falling back without an auto-built local database."
        )
        return None

    logger.info(f"Local BLAST database built successfully at '{resolved_db_path}'.")
    return resolved_db_path


class _BlastDiskCache:
    """
    Persistent, cross-run cache of BLAST results, backed by a small
    SQLite database (stdlib `sqlite3`, no new dependency). Exists
    specifically because remote BLAST is the single slowest call in
    GEPER -- a sequence that was already BLASTed in an earlier run
    (or an earlier, interrupted attempt at this run -- see the
    orchestrator's existing checkpoint/resume support) should never
    be resubmitted to NCBI.

    Safe by construction: any failure to open/read/write the cache
    (read-only filesystem, corrupted file, concurrent-writer lock
    contention) is caught and logged, and the caller falls back to
    treating it as a cache miss / a no-op write -- a broken disk
    cache must never be able to fail a BLAST search that would
    otherwise have succeeded.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._enabled = True
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS blast_cache ("
                    "  cache_key TEXT PRIMARY KEY,"
                    "  result_json TEXT NOT NULL,"
                    "  created_at REAL NOT NULL"
                    ")"
                )
        except Exception as exc:  # noqa: BLE001 - cache must never block BLAST
            logger.warning(
                f"Could not initialize BLAST disk cache at '{path}' ({exc}); "
                "continuing without cross-run BLAST caching."
            )
            self._enabled = False

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: search_many() reads/writes this
        # cache from a thread pool; access is still serialized by
        # self._lock, so this is safe.
        return sqlite3.connect(self.path, timeout=30, check_same_thread=False)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute("SELECT result_json FROM blast_cache WHERE cache_key = ?", (key,)).fetchone()
            if row is None:
                return None
            return json.loads(row[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"BLAST disk cache read failed ({exc}); treating as a cache miss.")
            return None

    def set(self, key: str, result: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO blast_cache (cache_key, result_json, created_at) VALUES (?, ?, ?)",
                    (key, json.dumps(result), time.time()),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"BLAST disk cache write failed ({exc}); result was still returned to the caller.")


class BLASTClient:
    """Runs BLAST+ searches (remote NCBI web service or local binary) for a sequence."""

    def __init__(
        self,
        mode: Optional[str] = None,
        local_db_path: Optional[str] = None,
        reference_fasta: Optional[str] = None,
        disabled: bool = False,
        enable_disk_cache: Optional[bool] = None,
        cache_dir: Optional[str] = None,
    ):
        # mode=None (the default) defers to CONFIG.api.BLAST_MODE
        # (GEPER_BLAST_MODE, default "auto"), so a bare `BLASTClient()`
        # -- or a caller like the orchestrator that just forwards
        # whatever it was given -- picks up the deployment's configured
        # backend priority without every call site needing to resolve
        # the env var itself.
        if mode is None:
            mode = CONFIG.api.BLAST_MODE
        mode = (mode or "auto").strip().lower()
        if mode not in ("remote", "local", "auto"):
            raise ValueError("BLASTClient mode must be 'remote', 'local', or 'auto'.")

        # AI-only mode: BLAST is disabled entirely. The client still
        # constructs successfully (so callers don't need special-case
        # branching), but search()/search_many() short-circuit to a
        # cheap, explicit "skipped" result with zero network/subprocess
        # activity -- see run().
        self.disabled = disabled

        requested_mode = mode
        self.local_db_path = local_db_path or CONFIG.api.BLAST_LOCAL_DB_PATH or os.environ.get("BLASTDB") or None
        self.reference_fasta = reference_fasta or CONFIG.api.BLAST_REFERENCE_FASTA or None

        # Auto-build support (requirement: "if the user provides a
        # FASTA reference, automatically create a BLAST database using
        # makeblastdb; do not rebuild if it already exists"). Runs
        # before mode resolution so a freshly-built database is
        # immediately visible to `_resolve_auto_mode`'s detection
        # below, not just on some later run. Best-effort: any failure
        # here (missing FASTA, missing makeblastdb, a build error) is
        # logged and this simply leaves `self.local_db_path` as it was
        # -- it never raises, so a broken auto-build attempt degrades
        # to remote/skip exactly like "no local database configured"
        # would.
        if self.reference_fasta and not disabled:
            built_db_path = ensure_local_blast_db(self.reference_fasta, self.local_db_path)
            if built_db_path:
                self.local_db_path = built_db_path

        if mode == "auto" and not disabled:
            mode = self._resolve_auto_mode(self.local_db_path)
            if mode == "remote":
                # "auto" fell through to remote -- but remote itself
                # has a hard dependency (Biopython) that may not be
                # installable in this environment (no PyPI access,
                # air-gapped/offline deployment, etc). In that case
                # there is genuinely no usable BLAST backend, so "auto"
                # degrades one more step to a graceful skip rather than
                # letting every search() call fail with a raised
                # exception later (requirement: "graceful skip if both
                # unavailable").
                biopython = check_pip_package_availability("biopython", import_name="Bio")
                if biopython is not PackageCheckStatus.PRESENT:
                    if biopython is PackageCheckStatus.NOT_CHECKED:
                        logger.warning(
                            "BLAST mode 'auto': no usable local blastn/database "
                            "found, and Biopython's availability was not checked "
                            "in this environment (auto-install is disabled under "
                            "pytest), so whether remote BLAST could run here is "
                            "unknown rather than ruled out. BLAST will be skipped "
                            "gracefully for this pipeline instance -- "
                            "interpretation continues without BLAST evidence."
                        )
                    else:
                        logger.warning(
                            "BLAST mode 'auto': no usable local blastn/database "
                            "found, and Biopython (required for remote NCBI "
                            "BLAST) could not be imported or installed in this "
                            "environment. BLAST will be skipped gracefully for "
                            "this pipeline instance -- interpretation continues "
                            "without BLAST evidence."
                        )
                    mode = "skip"
            logger.info(
                f"BLAST mode 'auto' resolved to '{mode}' "
                + {
                    "local": "(local blastn + database found).",
                    "remote": "(no usable local blastn/database found; using remote NCBI BLAST).",
                    "skip": "(no usable local or remote BLAST backend found).",
                }[mode]
            )
        elif mode == "local" and not self.local_db_path:
            raise ValueError("local_db_path is required when mode='local'.")

        self.mode = mode
        self.requested_mode = requested_mode

        if self.mode == "remote" and not disabled:
            # Fail fast / auto-install here, at construction, rather
            # than silently discovering it's missing on the first
            # variant that reaches the BLAST stage -- same "surface
            # setup problems immediately" philosophy as the startup
            # model validation in the orchestrator.
            ensure_pip_package_available("biopython", import_name="Bio")

        # Memoizes search() results by exact (mode, program, database,
        # max_hits, sequence) for this process's lifetime.
        self._result_cache: Dict[str, Dict[str, Any]] = {}

        if enable_disk_cache is None:
            enable_disk_cache = CONFIG.api.BLAST_DISK_CACHE_ENABLED
        self._disk_cache: Optional[_BlastDiskCache] = None
        if enable_disk_cache and not disabled:
            cache_path = os.path.join(cache_dir or CONFIG.CACHE_DIR, "blast_cache.sqlite")
            self._disk_cache = _BlastDiskCache(cache_path)

        # Bounds how many *distinct* sequences search_many() will
        # submit concurrently. Remote BLAST is a shared NCBI service,
        # so this defaults to a small, polite number rather than
        # "as many as we have sequences" -- local BLAST has no such
        # courtesy concern and defaults higher (bounded by CPU count).
        self.max_concurrent = (
            CONFIG.api.BLAST_MAX_CONCURRENT_LOCAL if self.mode == "local" else CONFIG.api.BLAST_MAX_CONCURRENT_REMOTE
        )

    @staticmethod
    def _resolve_auto_mode(local_db_path: Optional[str]) -> str:
        """
        Decide what 'auto' means in this environment: 'local' if a
        `blastn` binary is on PATH AND a database path is configured
        AND at least one of that database's expected files exists on
        disk; 'remote' otherwise. Deliberately conservative -- a wrong
        guess of 'local' would surface as a hard, confusing subprocess
        failure on the first variant, whereas a wrong guess of
        'remote' just means paying the (already-existing, already
        cached/parallelized) remote cost, so any ambiguity resolves
        toward 'remote'. (A further "no usable remote backend either"
        case is handled by the caller in `__init__`, which can degrade
        this "remote" result one more step to "skip".)
        """
        if not local_db_path:
            return "remote"
        if shutil.which("blastn") is None:
            return "remote"
        return "local" if _has_local_db_files(local_db_path) else "remote"

    def _mode_identity(self) -> str:
        """See `_cache_key` docstring: 'local' identity includes the DB path."""
        if self.mode == "local":
            return f"local:{self.local_db_path}"
        return "remote"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def search(
        self,
        sequence: str,
        program: str = "blastn",
        database: str = "nt",
        max_hits: int = 5,
    ) -> Dict[str, Any]:
        """Run a BLAST search and return a normalized hit summary."""
        if self.disabled:
            return self._disabled_result()
        if self.mode == "skip":
            # "auto" resolved to neither a usable local database nor a
            # usable remote backend -- see __init__. Same zero-cost,
            # explicit short-circuit as `disabled`, just a different
            # human-readable reason.
            return self._skipped_result()

        key = _cache_key(self._mode_identity(), program, database, max_hits, sequence)

        cached = self._result_cache.get(key)
        if cached is not None:
            logger.info(f"BLAST in-memory cache hit for a {len(sequence)}bp sequence -- skipping a duplicate search.")
            return cached

        if self._disk_cache is not None:
            disk_hit = self._disk_cache.get(key)
            if disk_hit is not None:
                logger.info(
                    f"BLAST disk cache hit for a {len(sequence)}bp sequence "
                    "(result reused from a previous run) -- skipping NCBI submission."
                )
                self._result_cache[key] = disk_hit
                return disk_hit

        if self.mode == "remote":
            result = self._search_remote(sequence, program, database, max_hits)
        else:
            result = self._search_local(sequence, program, max_hits)

        self._result_cache[key] = result
        if self._disk_cache is not None:
            self._disk_cache.set(key, result)
        return result

    def search_many(
        self,
        sequences: List[str],
        program: str = "blastn",
        database: str = "nt",
        max_hits: int = 5,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Search a batch of sequences, deduplicated, with distinct
        sequences submitted concurrently (bounded by
        `self.max_concurrent`). Returns a dict mapping each *unique*
        input sequence to its normalized result (identical in shape
        and content to what `search()` would return for that
        sequence -- this is purely a scheduling optimization, not a
        different code path for the actual BLAST call itself: every
        worker thread calls the same `search()` method above, so
        caching, disk persistence, and result normalization all still
        apply the same way per-sequence).

        Safe to call with disabled=True (returns the disabled/no-op
        result for every sequence) or with a single sequence (behaves
        like one `search()` call, no thread pool overhead beyond one
        worker).
        """
        unique_sequences = list(dict.fromkeys(sequences))  # de-dupe, preserve order
        if not unique_sequences:
            return {}

        if self.disabled:
            return {seq: self._disabled_result() for seq in unique_sequences}
        if self.mode == "skip":
            return {seq: self._skipped_result() for seq in unique_sequences}

        results: Dict[str, Dict[str, Any]] = {}
        workers = max(1, min(self.max_concurrent, len(unique_sequences)))

        logger.info(
            f"BLAST batch: {len(unique_sequences)} distinct sequence(s) "
            f"(from {len(sequences)} requested) via '{self.mode}' mode, "
            f"up to {workers} concurrent."
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_seq = {
                executor.submit(self.search, seq, program, database, max_hits): seq for seq in unique_sequences
            }
            for future in as_completed(future_to_seq):
                seq = future_to_seq[future]
                try:
                    results[seq] = future.result()
                except ExternalAPIError as exc:
                    logger.error(f"BLAST batch: search failed for a sequence ({exc}).")
                    results[seq] = {
                        "hits": [],
                        "hit_count": 0,
                        "skipped": True,
                        "reason": str(exc),
                    }

        return results

    @staticmethod
    def _disabled_result() -> Dict[str, Any]:
        return {
            "hits": [],
            "hit_count": 0,
            "skipped": True,
            "reason": "BLAST disabled (AI-only mode)",
        }

    @staticmethod
    def _skipped_result() -> Dict[str, Any]:
        return {
            "hits": [],
            "hit_count": 0,
            "skipped": True,
            "reason": (
                "BLAST unavailable: no usable local blastn/database and no "
                "usable remote BLAST backend (Biopython) in this environment; "
                "skipped gracefully."
            ),
        }

    # ------------------------------------------------------------------
    # Remote (NCBI-hosted) BLAST via Biopython
    # ------------------------------------------------------------------
    def _search_remote(self, sequence: str, program: str, database: str, max_hits: int) -> Dict[str, Any]:
        biopython = check_pip_package_availability("biopython", import_name="Bio")
        if biopython is PackageCheckStatus.NOT_CHECKED:
            raise ExternalAPIError(
                "Remote BLAST requires Biopython, whose availability was not "
                "checked in this environment (auto-install is disabled under "
                "pytest). It is not confirmed missing -- it was never looked "
                "for. Install it with `pip install biopython`, or run outside "
                "the test harness, to find out."
            )
        if biopython is PackageCheckStatus.ABSENT:
            raise ExternalAPIError(
                "Remote BLAST requires Biopython, and automatic "
                "installation ('pip install biopython') did not succeed "
                "in this environment -- check network access to "
                "pypi.org, or install it yourself with "
                "`pip install biopython`."
            )
        from Bio.Blast import NCBIWWW, NCBIXML

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.api.MAX_RETRIES + 1):
            try:
                logger.info(
                    f"Submitting remote BLAST ({program} vs {database}) for a "
                    f"{len(sequence)}bp sequence (attempt {attempt})..."
                )
                result_handle = NCBIWWW.qblast(program, database, sequence, hitlist_size=max_hits)
                record = NCBIXML.read(result_handle)
                return self._normalize_biopython_record(record, max_hits)
            except Exception as exc:  # noqa: BLE001 - network/service errors vary widely
                last_error = exc
                logger.warning(f"Remote BLAST attempt {attempt} failed: {exc}")
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)

        logger.warning(
            f"Remote BLAST failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: `last_error` here is whatever Biopython's `NCBIWWW.qblast`
        # raised, which for a real network failure can stringify to
        # include NCBI's request URL/host -- same leak class as every
        # other provider in this document, reaching `report/
        # clinical_report_builder.py`/`report/report_generator.py` via
        # `_run_blast_stage`'s own `reason`/`error` fields (see that
        # method's comment in `pipeline/orchestrator.py`). Full detail
        # goes to the log line above only.
        raise ExternalAPIError(f"Remote BLAST failed after {CONFIG.api.MAX_RETRIES} attempts")

    @staticmethod
    def _normalize_biopython_record(record, max_hits: int) -> Dict[str, Any]:
        hits: List[Dict[str, Any]] = []
        for alignment in record.alignments[:max_hits]:
            hsp = alignment.hsps[0] if alignment.hsps else None
            hits.append(
                {
                    "hit_id": alignment.hit_id,
                    "title": alignment.title,
                    "length": alignment.length,
                    "e_value": hsp.expect if hsp else None,
                    "identity": f"{hsp.identities}/{hsp.align_length}" if hsp else None,
                    "score": hsp.score if hsp else None,
                }
            )
        return {"mode": "remote", "database": record.database, "hits": hits, "hit_count": len(hits)}

    # ------------------------------------------------------------------
    # Local BLAST+ command-line tools
    # ------------------------------------------------------------------
    def _search_local(self, sequence: str, program: str, max_hits: int) -> Dict[str, Any]:
        query_fasta = f">query\n{sequence}\n"
        command = [
            program,
            "-db",
            self.local_db_path,
            "-outfmt",
            "6 sseqid pident length evalue bitscore stitle",
            "-max_target_seqs",
            str(max_hits),
        ]
        try:
            proc = subprocess.run(
                command,
                input=query_fasta,
                capture_output=True,
                text=True,
                timeout=CONFIG.api.REQUEST_TIMEOUT_SECS * 4,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ExternalAPIError(
                f"Local BLAST binary '{program}' not found on PATH. Install "
                f"NCBI BLAST+ command line tools to use mode='local'."
            ) from exc
        except subprocess.CalledProcessError as exc:
            # Round 28: `exc.stderr` is the local `blastn` process's raw
            # error output, which routinely embeds `self.local_db_path`
            # (a local filesystem path) -- e.g. "BLAST Database error:
            # No alias or index file found ... in search path
            # [/local/path]". Same local-path-leak class rounds 18-22
            # fixed for SpliceBERT's checkpoint_dir and MMSplice's
            # h5_path/package_dir, reaching the report via
            # `_run_blast_stage`'s `reason`/`error` fields. Full stderr
            # goes to the log only; what's raised names the failure, not
            # the path.
            logger.warning(f"Local BLAST search failed (exit {exc.returncode}): {exc.stderr}", exc_info=True)
            raise ExternalAPIError(f"Local BLAST search failed (exit code {exc.returncode}).") from exc
        except subprocess.TimeoutExpired as exc:
            # Round 28: `str(exc)` embeds the full command list, which
            # includes `-db` and `self.local_db_path` -- same local-path
            # leak as the `CalledProcessError` branch above.
            logger.warning(f"Local BLAST search timed out: {exc}", exc_info=True)
            raise ExternalAPIError(
                f"Local BLAST search timed out after {CONFIG.api.REQUEST_TIMEOUT_SECS * 4}s."
            ) from exc

        hits = []
        for line in proc.stdout.strip().splitlines():
            fields = line.split("\t")
            if len(fields) < 6:
                continue
            hits.append(
                {
                    "hit_id": fields[0],
                    "identity_pct": float(fields[1]),
                    "alignment_length": int(fields[2]),
                    "e_value": float(fields[3]),
                    "bit_score": float(fields[4]),
                    "title": fields[5],
                }
            )
        return {"mode": "local", "database": self.local_db_path, "hits": hits, "hit_count": len(hits)}
