"""
api/main.py
────────────
GEPER production FastAPI application.

Endpoints
---------
GET  /health                 — liveness probe
GET  /api/v1/config          — return active configuration (sanitised)
POST /api/v1/upload/fastq    — upload a FASTQ (plain or .gz) file
POST /api/v1/upload/fasta    — upload a FASTA file
POST /api/v1/pipeline/start  — start a pipeline run (async background task)
GET  /api/v1/pipeline/{run_id}/status   — run status + stage progress
GET  /api/v1/pipeline/{run_id}/progress — lightweight % progress
GET  /api/v1/pipeline/{run_id}/report   — download final report (JSON/HTML)
GET  /api/v1/pipeline/{run_id}/log      — download run log
DELETE /api/v1/pipeline/{run_id}        — cancel / delete run artefacts

All long-running work is executed in a thread-pool background task so the
API stays non-blocking.

Configuration via environment variables:
  GEPER_CONFIG_PATH     Path to YAML config (default: config/default.yaml)
  GEPER_OUTPUT_DIR      Root directory for pipeline outputs (default: /tmp/geper_runs)
  GEPER_UPLOAD_DIR      Directory for uploaded files (default: /tmp/geper_uploads)
  GEPER_MAX_UPLOAD_MB   Max upload size in MB (default: 2048)
  GEPER_LOG_LEVEL       Logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

# ── Adjust sys.path so the API can import the pipeline package ────────────────
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.orchestration.runner import PipelineRunner
from api.run_store import RunStore

# ─── Configuration ────────────────────────────────────────────────────────────

_CONFIG_PATH = os.getenv("GEPER_CONFIG_PATH", str(_PROJECT_ROOT / "config" / "default.yaml"))
_OUTPUT_DIR = Path(os.getenv("GEPER_OUTPUT_DIR", "/tmp/geper_runs"))
_UPLOAD_DIR = Path(os.getenv("GEPER_UPLOAD_DIR", "/tmp/geper_uploads"))
_MAX_UPLOAD_MB = int(os.getenv("GEPER_MAX_UPLOAD_MB", "2048"))
_LOG_LEVEL = os.getenv("GEPER_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("geper.api")

_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _load_config() -> Dict:
    try:
        with open(_CONFIG_PATH) as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning("Config not found at %s — using empty config", _CONFIG_PATH)
        return {}
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return {}


_PIPELINE_CONFIG: Dict = _load_config()

# ─── In-memory run registry + SQLite persistence ─────────────────────────────
# _RUNS is a thin in-memory cache; RunStore writes every change to SQLite.

_RUN_STORE = RunStore()
_RUNS: Dict[str, Dict[str, Any]] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="geper_worker")

# GAP 4: process group registry for cancellation
_PROCESS_GROUPS: Dict[str, int] = {}  # run_id → pgid

# ─── API key authentication (GAP 2) ──────────────────────────────────────────


def _load_api_keys() -> Optional[Set[str]]:
    """Load valid API keys from GEPER_API_KEYS env var.

    Returns None if not set (dev mode — allow all requests).
    """
    raw = os.getenv("GEPER_API_KEYS", "")
    if not raw:
        return None
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    return keys if keys else None


_API_KEYS: Optional[Set[str]] = _load_api_keys()
_CORS_ORIGINS_ENV: Optional[str] = os.getenv(
    "GEPER_CORS_ORIGINS"
)  # None if unset; "*" applied later
_DEV_INSECURE: bool = os.getenv("GEPER_DEV_INSECURE", "") == "1"

if _API_KEYS is None:
    logger.warning(
        "GEPER_API_KEYS is not set — running in dev mode with no authentication. "
        "Set GEPER_API_KEYS=key1,key2 before deploying to production."
    )


def _refuse_insecure_defaults_unless_opted_in() -> None:
    """FIX #3: a startup warning is not a control -- it arrives after the
    decision to run without auth/CORS restriction has already been made.
    Refuse to start with the insecure defaults (no auth, CORS "*") unless
    GEPER_DEV_INSECURE=1 is set explicitly, so a deployer who simply
    forgets GEPER_API_KEYS/GEPER_CORS_ORIGINS gets a hard failure instead
    of a log line that's easy to miss on a deploy console.
    """
    if _DEV_INSECURE:
        return
    missing = [
        name
        for name, value in (
            ("GEPER_API_KEYS", _API_KEYS),
            ("GEPER_CORS_ORIGINS", _CORS_ORIGINS_ENV),
        )
        if value is None
    ]
    if missing:
        sys.stderr.write(
            "ERROR: refusing to start with insecure defaults -- "
            f"{' and '.join(missing)} not set. Set them before a real deployment, "
            "or set GEPER_DEV_INSECURE=1 to run insecurely on purpose (dev/test only).\n"
        )
        sys.exit(1)


_refuse_insecure_defaults_unless_opted_in()


async def _require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency: validate X-Api-Key header against GEPER_API_KEYS.

    If GEPER_API_KEYS is not set, allows all requests (dev mode).
    Raises HTTP 401 if an invalid or missing key is supplied in protected mode.
    """
    if _API_KEYS is None:
        # Dev mode — no auth required
        return
    if not x_api_key or x_api_key not in _API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Provide a valid key in the X-Api-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bij AI Genomics Pipeline API",
    description=(
        "Production API for the Bij AI clinical genomics pipeline. "
        "Accepts FASTQ inputs and returns annotated variant reports. "
        "DISCLAIMER: Bij AI assists qualified clinicians and pathologists; it produces a draft "
        "classification requiring qualified human review and final sign-off before any clinical "
        "use, and does not independently provide final clinical interpretation."
    ),
    version="8.0.0",
    contact={"name": "Geper Team"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {"name": "health", "description": "Liveness and readiness probes"},
        {"name": "uploads", "description": "File upload endpoints"},
        {"name": "pipeline", "description": "Pipeline control and monitoring"},
        {"name": "config", "description": "Configuration inspection"},
    ],
)

# CORS — restrict origins in production via GEPER_CORS_ORIGINS env var
_cors_origins = (_CORS_ORIGINS_ENV or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Exception handlers ───────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = str(uuid.uuid4())
    logger.exception(
        "Unhandled exception [%s] on %s %s: %s",
        error_id,
        request.method,
        request.url,
        str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "InternalServerError",
            "error_id": error_id,
            "detail": "An internal error occurred. Contact support with error ID if the problem persists.",
        },
    )


# ─── Pydantic models ──────────────────────────────────────────────────────────


class PipelineStartRequest(BaseModel):
    """Request body for POST /api/v1/pipeline/start."""

    fastq_r1_path: str = Field(..., description="Path to uploaded R1 FASTQ file")
    reference_fasta_path: str = Field(..., description="Absolute path to reference FASTA")
    fastq_r2_path: Optional[str] = Field(None, description="Path to uploaded R2 FASTQ (paired-end)")
    sample_id: str = Field("SAMPLE", description="Sample identifier")
    config_overrides: Optional[Dict[str, Any]] = Field(
        None, description="Optional per-run config key overrides (merged with base config)"
    )
    pedigree_json: Optional[str] = Field(
        None, description="Optional path to pedigree/phenotype JSON sidecar file"
    )

    @field_validator("sample_id")
    @classmethod
    def _valid_sample_id(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("sample_id must contain only alphanumerics, hyphens, underscores")
        return v[:64]  # cap length


class RunStatusResponse(BaseModel):
    run_id: str
    sample_id: str
    status: str  # pending | running | completed | failed | cancelled
    stage: Optional[str] = None
    progress_pct: float = 0.0
    stages_completed: list = []
    stages_failed: list = []
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    error: Optional[str] = None
    report_json_path: Optional[str] = None
    report_html_path: Optional[str] = None


# ─── Stage-to-progress mapping ───────────────────────────────────────────────

_STAGE_PROGRESS = {
    "fastq_validation": 10.0,
    "qc": 20.0,
    "alignment": 50.0,
    "variant_calling": 70.0,
    "annotation": 85.0,
    "reporting": 100.0,
}


# ─── Background worker ───────────────────────────────────────────────────────


def _run_pipeline_sync(run_id: str, req: PipelineStartRequest) -> None:
    """Synchronous pipeline execution — runs in thread pool."""
    run = _RUNS[run_id]
    run["status"] = "running"
    run["started_at"] = datetime.now(timezone.utc).isoformat()
    _RUN_STORE.update(run_id, status="running", started_at=run["started_at"])
    t0 = time.time()

    try:
        cfg = {**_PIPELINE_CONFIG}
        if req.config_overrides:
            cfg.update(req.config_overrides)

        out_dir = str(_OUTPUT_DIR)

        runner = PipelineRunner(cfg=cfg)

        # GAP 4: register kill callback so DELETE can send SIGTERM/SIGKILL
        def _kill_cb(popen_obj):
            try:
                pgid = os.getpgid(popen_obj.pid)
                _PROCESS_GROUPS[run_id] = pgid
            except Exception:
                pass

        runner.register_kill_callback(_kill_cb)

        result = runner.run(
            fastq_r1=req.fastq_r1_path,
            reference_fasta=req.reference_fasta_path,
            output_dir=out_dir,
            fastq_r2=req.fastq_r2_path or None,
            sample_id=req.sample_id,
            pedigree_json=req.pedigree_json or None,
        )

        run["status"] = "completed"
        run["stages_completed"] = result.stages_completed
        run["progress_pct"] = 100.0
        run["stage"] = "reporting"

        if result.log_path:
            run["log_path"] = result.log_path

        work_dir = Path(out_dir) / req.sample_id
        json_report = work_dir / "reporting" / "report.json"
        html_report = work_dir / "reporting" / "report.html"
        if json_report.exists():
            run["report_json_path"] = str(json_report)
        if html_report.exists():
            run["report_html_path"] = str(html_report)

        _RUN_STORE.update(
            run_id,
            status="completed",
            stages_completed=result.stages_completed,
            progress_pct=100.0,
            stage="reporting",
            log_path=run.get("log_path"),
            report_json_path=run.get("report_json_path"),
            report_html_path=run.get("report_html_path"),
        )

    except Exception as exc:
        logger.exception("[%s] Pipeline failed", run_id)
        run["status"] = "failed"
        run["error"] = str(exc)
        run["stages_failed"].append(run.get("stage", "unknown"))
        _RUN_STORE.update(run_id, status="failed", error=str(exc))
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        run["elapsed_seconds"] = round(time.time() - t0, 2)
        _RUN_STORE.update(
            run_id,
            finished_at=run["finished_at"],
            elapsed_seconds=run["elapsed_seconds"],
        )
        _PROCESS_GROUPS.pop(run_id, None)


# ─── Health endpoints ─────────────────────────────────────────────────────────


@app.get("/health", tags=["health"], summary="Liveness probe")
async def health() -> Dict:
    """Returns 200 OK if the API process is alive."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": app.version,
    }


@app.get("/api/v1/ready", tags=["health"], summary="Readiness probe")
async def ready() -> Dict:
    """Returns 200 if the API is ready to accept pipeline requests."""
    return {
        "status": "ready",
        "active_runs": sum(1 for r in _RUNS.values() if r.get("status") == "running"),
        "config_loaded": bool(_PIPELINE_CONFIG),
    }


# ─── Config endpoint ──────────────────────────────────────────────────────────


# History: this redaction was originally a name-based DENYLIST (redact keys
# containing "key"/"token"/"secret"/"password"), first applied one dict-level
# deep only, then made recursive. Both versions were default-ALLOW: an
# unrecognised field name leaked by default. Confirmed leaking under the
# denylist: auth_credential, oauth_bearer, private_cert -- none matched the
# keyword list. That's an unbounded failure mode (anything not yet thought of
# leaks), so the model is inverted here to an explicit, per-field ALLOWLIST
# (default-DENY): only fields named in _CONFIG_ALLOWLIST are ever returned;
# everything else is "***REDACTED***", including fields added to
# config/default.yaml in the future that nobody remembers to allowlist.
#
# The allowlist is deliberately per-FIELD, not per-section: a section-level
# allow would silently expose any field later added under that section,
# reinstating the exact defect this inversion exists to remove.
#
# Filesystem paths (tsv_gz_path, vcf_path, cache_dir, refseq_gff, index_dir,
# db_path, vep.cache_dir/dir_plugins/cadd_data/revel_data/alphamissense_data/
# fasta, reporting.output_dir, api.host, ...) are deliberately NOT allowlisted
# even though they aren't secrets: they reveal server deployment structure,
# the same leak class the original 500-handler information-disclosure fix
# addressed (str(request.url)). config_path/output_dir/upload_dir below (the
# three fields returned alongside "config", not inside it) are the same
# category and are redacted for the same reason -- see hive dispatch
# 2026-09-02 "PART 1 APPROVED: Allowlist rulings" for the bucket-by-bucket
# policy decision this list encodes.
_CONFIG_ALLOWLIST: Dict[str, Any] = {
    "clinvar": {"enabled": True},
    "gnomad": {
        "enabled": True,
        "graphql_dataset": True,
        "timeout": True,
        "max_retries": True,
        "retry_backoff_secs": True,
        "max_concurrent": True,
        "cache_ttl_hours": True,
    },
    "rna_analysis": {"enabled": True, "auto_fetch_mt_gff3": True},
    "acmg_thresholds": {
        "ba1_af": True,
        "bs1_af": True,
        "pm2_af_max": True,
        "pp3_cadd_phred": True,
        "pp3_revel": True,
        "pp3_spliceai": True,
        "pp3_alphamissense": True,
        "bp4_cadd_phred": True,
        "bp4_revel": True,
        "bp4_spliceai": True,
    },
    "evidence_engine": {
        "weight_acmg": True,
        "weight_clinvar": True,
        "weight_computational": True,
        "weight_ai": True,
    },
    "reporting": {"generate_pdf": True},
    "api": {"log_level": True, "cors_origins": True, "port": True},
    "dnabert2": {"model_name": True, "device": True, "batch_size": True},
    "esm2": {"model_name": True, "device": True, "batch_size": True},
    "alignment": {"aligner": True, "threads": True, "preset": True},
    "variant_calling": {
        "threads": True,
        "min_base_quality": True,
        "min_mapping_quality": True,
        "min_alternate_fraction": True,
        "min_alternate_count": True,
        "filter_min_qual": True,
        "filter_min_depth": True,
    },
    "qc": {
        "stop_on_failure": True,
        "max_dup_sample": True,
        "thresholds": {
            "min_mean_quality": True,
            "max_n_fraction": True,
            "min_gc_fraction": True,
            "max_gc_fraction": True,
            "max_adapter_fraction": True,
            "max_duplicate_fraction": True,
            "min_total_reads": True,
            "min_read_length": True,
        },
    },
    "blast": {
        "enabled": True,
        "evalue": True,
        "max_target_seqs": True,
        "word_size": True,
        "timeout": True,
        "output_format": True,
        "threads": True,
    },
    "gnomad_constraint": {"loeuf_threshold": True, "pli_threshold": True},
    "clingen": {
        "enabled": True,
        "api_enabled": True,
        "api_endpoint": True,
        "timeout": True,
        "max_retries": True,
    },
    "hotspot": {"enabled": True, "min_stars": True},
    "vep": {"enabled": True, "timeout": True},
}


def _apply_config_allowlist(cfg: Dict, allowed: Dict) -> Dict:
    """Recursively redact everything not explicitly named in `allowed`.

    `allowed[k] is True` exposes a scalar leaf as-is. `allowed[k]` being a
    dict descends into `cfg[k]` (also a dict) under that nested allowlist.
    Anything else -- a key absent from `allowed`, or a shape mismatch
    between `cfg` and `allowed` at that key -- redacts to "***REDACTED***".
    Default-deny: an unrecognised field, including one added to the config
    file after this list was written, is never exposed.
    """
    result: Dict = {}
    for k, v in cfg.items():
        spec = allowed.get(k)
        if isinstance(v, dict):
            result[k] = (
                _apply_config_allowlist(v, spec) if isinstance(spec, dict) else "***REDACTED***"
            )
        else:
            result[k] = v if spec is True else "***REDACTED***"
    return result


@app.get("/api/v1/config", tags=["config"], summary="Return active configuration")
async def get_config(_auth: None = Depends(_require_api_key)) -> Dict:
    """Return the active pipeline configuration, allowlisted field-by-field.

    Only fields named in _CONFIG_ALLOWLIST are returned as-is; everything
    else -- secrets, filesystem paths, and any future/unrecognised field --
    is redacted. config_path/output_dir/upload_dir are themselves
    filesystem paths (deployment structure) and are redacted for the same
    reason, not exposed just because they sit outside the "config" dict.
    """
    safe_cfg = _apply_config_allowlist(_PIPELINE_CONFIG, _CONFIG_ALLOWLIST)
    return {
        "config": safe_cfg,
        "config_path": "***REDACTED***",
        "output_dir": "***REDACTED***",
        "upload_dir": "***REDACTED***",
    }


# ─── Upload endpoints ─────────────────────────────────────────────────────────


async def _save_upload(file: UploadFile, allowed_suffixes: tuple) -> str:
    """Save an uploaded file to _UPLOAD_DIR and return its path."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    suffix = Path(file.filename).suffix.lower()
    # Handle double extensions like .fastq.gz
    name_lower = file.filename.lower()
    if not any(name_lower.endswith(s) for s in allowed_suffixes):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {allowed_suffixes}",
        )

    # Read with size check
    max_bytes = _MAX_UPLOAD_MB * 1024 * 1024
    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex}_{Path(file.filename).name}"

    written = 0
    with open(dest, "wb") as out:
        chunk_size = 1024 * 1024  # 1 MB chunks
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum size of {_MAX_UPLOAD_MB} MB",
                )
            out.write(chunk)

    logger.info("Uploaded file: %s → %s (%d bytes)", file.filename, dest.name, written)
    return str(dest)


@app.post(
    "/api/v1/upload/fastq",
    tags=["uploads"],
    summary="Upload a FASTQ file",
    status_code=status.HTTP_201_CREATED,
)
async def upload_fastq(
    file: UploadFile = File(...),
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Upload a FASTQ file (.fastq, .fq, .fastq.gz, .fq.gz).

    Returns the server-side path to use in the pipeline start request.
    """
    path = await _save_upload(file, (".fastq", ".fq", ".fastq.gz", ".fq.gz"))
    return {"upload_id": Path(path).name, "path": path, "filename": file.filename}


@app.post(
    "/api/v1/upload/fasta",
    tags=["uploads"],
    summary="Upload a FASTA file",
    status_code=status.HTTP_201_CREATED,
)
async def upload_fasta(
    file: UploadFile = File(...),
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Upload a FASTA file (.fasta, .fa, .fna, .fasta.gz).

    Used for reference genome uploads.
    """
    path = await _save_upload(file, (".fasta", ".fa", ".fna", ".fasta.gz"))
    return {"upload_id": Path(path).name, "path": path, "filename": file.filename}


# ─── Pipeline control endpoints ───────────────────────────────────────────────


@app.post(
    "/api/v1/pipeline/start",
    tags=["pipeline"],
    summary="Start a pipeline run",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_pipeline(
    req: PipelineStartRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Queue a FASTQ → Report pipeline run.

    Validates all required paths exist before queuing.
    Returns a run_id for status polling.
    """
    # Validate that all supplied paths are inside approved directories
    _validate_path_in_roots(req.fastq_r1_path, _UPLOAD_DIR, _OUTPUT_DIR)
    if req.fastq_r2_path:
        _validate_path_in_roots(req.fastq_r2_path, _UPLOAD_DIR, _OUTPUT_DIR)
    # Reference FASTA may live in the upload dir or a pre-configured reference dir;
    # accept any path under _UPLOAD_DIR or _OUTPUT_DIR for uploaded references.
    _validate_path_in_roots(req.reference_fasta_path, _UPLOAD_DIR, _OUTPUT_DIR)

    # FIX 8 — validate config_overrides against explicit allowlist
    _ALLOWED_OVERRIDE_SECTIONS = frozenset(
        {
            "qc",
            "acmg_thresholds",
            "evidence_engine",
            "evidence_thresholds",
            "blast",
            "pgx",
            "ancestry",
        }
    )
    if req.config_overrides:
        rejected = [k for k in req.config_overrides if k not in _ALLOWED_OVERRIDE_SECTIONS]
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"config_overrides contains blocked section(s): {rejected}. "
                    f"Allowed sections: {sorted(_ALLOWED_OVERRIDE_SECTIONS)}"
                ),
            )

    # FIX 11 — validate optional pedigree sidecar path
    if req.pedigree_json:
        _validate_path_in_roots(req.pedigree_json, _UPLOAD_DIR, _OUTPUT_DIR)

    # Validate inputs exist
    if not Path(req.fastq_r1_path).exists():
        raise HTTPException(status_code=400, detail=f"R1 FASTQ not found: {req.fastq_r1_path}")
    if not Path(req.reference_fasta_path).exists():
        raise HTTPException(
            status_code=400,
            detail=f"Reference FASTA not found: {req.reference_fasta_path}",
        )
    if req.fastq_r2_path and not Path(req.fastq_r2_path).exists():
        raise HTTPException(status_code=400, detail=f"R2 FASTQ not found: {req.fastq_r2_path}")

    run_id = uuid.uuid4().hex
    _RUNS[run_id] = {
        "run_id": run_id,
        "sample_id": req.sample_id,
        "status": "pending",
        "stage": None,
        "progress_pct": 0.0,
        "stages_completed": [],
        "stages_failed": [],
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "error": None,
        "report_json_path": None,
        "report_html_path": None,
        "fastq_r1": req.fastq_r1_path,
        "fastq_r2": req.fastq_r2_path,
        "reference_fasta": req.reference_fasta_path,
    }
    _RUN_STORE.create(run_id, _RUNS[run_id])

    # ISSUE 4 FIX: submit directly to the ThreadPoolExecutor rather than via
    # asyncio.get_running_loop().run_in_executor(). The latter requires a
    # running *asyncio* event loop specifically, and breaks with
    # `RuntimeError: no running event loop` when this ASGI app is driven by
    # a trio event loop instead (e.g. under anyio's trio backend, or any
    # trio-based ASGI server) — even though FastAPI/Starlette themselves
    # are backend-agnostic via anyio. ThreadPoolExecutor.submit() needs no
    # event loop at all, so this works identically under asyncio and trio.
    future = _EXECUTOR.submit(_run_pipeline_sync, run_id, req)

    def _on_done(fut):
        """Catch thread exceptions and mark run as failed — must not raise itself."""
        try:
            exc = fut.exception()
            if exc and run_id in _RUNS:
                _RUNS[run_id]["status"] = "failed"
                _RUNS[run_id]["error"] = str(exc)
        except Exception:
            pass

    future.add_done_callback(_on_done)

    logger.info("Pipeline queued: run_id=%s sample=%s", run_id, req.sample_id)
    return {
        "run_id": run_id,
        "status": "pending",
        "message": "Pipeline queued. Poll /api/v1/pipeline/{run_id}/status for updates.",
        "status_url": f"/api/v1/pipeline/{run_id}/status",
        "progress_url": f"/api/v1/pipeline/{run_id}/progress",
    }


def _validate_path_in_roots(path_str: str, *allowed_roots: Path) -> Path:
    """Resolve *path_str* to a canonical absolute path and verify it is inside
    one of *allowed_roots*.

    Raises ``HTTPException(400)`` for:
    - paths that resolve outside every allowed root (traversal / absolute escape)
    - paths that do not exist

    Args:
        path_str:      Raw path string from user input.
        *allowed_roots: Directories the path must be inside.

    Returns:
        Resolved ``Path`` object.
    """
    try:
        candidate = Path(path_str).resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid path: {exc}")

    for root in allowed_roots:
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
            # Path is inside this root — allow it
            return candidate
        except ValueError:
            continue

    raise HTTPException(
        status_code=400,
        detail=(
            f"Path '{path_str}' is not within an approved directory. "
            f"Allowed roots: {[str(r) for r in allowed_roots]}"
        ),
    )


def _get_run(run_id: str) -> Dict:
    run = _RUNS.get(run_id)
    if not run:
        # Fall back to persistent store (e.g. after API restart)
        run = _RUN_STORE.get(run_id)
        if run:
            _RUNS[run_id] = run  # re-hydrate cache
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run


@app.get(
    "/api/v1/pipeline/{run_id}/status",
    tags=["pipeline"],
    summary="Get full pipeline run status",
)
async def pipeline_status(
    run_id: str, _auth: None = Depends(_require_api_key)
) -> RunStatusResponse:
    """Return detailed status for a pipeline run."""
    run = _get_run(run_id)

    # Update progress from checkpoint if running
    if run["status"] == "running":
        _refresh_progress_from_checkpoint(run_id, run)

    return RunStatusResponse(**{k: run[k] for k in RunStatusResponse.model_fields if k in run})


@app.get(
    "/api/v1/pipeline/{run_id}/progress",
    tags=["pipeline"],
    summary="Lightweight progress poll",
)
async def pipeline_progress(run_id: str, _auth: None = Depends(_require_api_key)) -> Dict:
    """Return just the current progress percentage and status (lightweight poll)."""
    run = _get_run(run_id)
    if run["status"] == "running":
        _refresh_progress_from_checkpoint(run_id, run)
    return {
        "run_id": run_id,
        "status": run["status"],
        "stage": run.get("stage"),
        "progress_pct": run["progress_pct"],
    }


@app.get(
    "/api/v1/pipeline/{run_id}/report",
    tags=["pipeline"],
    summary="Download the final report",
)
async def download_report(
    run_id: str,
    format: str = "html",
    _auth: None = Depends(_require_api_key),
) -> FileResponse:
    """Download the final HTML or JSON report for a completed run.

    Query params:
      format: "html" (default) or "json"
    """
    run = _get_run(run_id)
    if run["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Run is {run['status']}; report not available until completed",
        )

    key = "report_html_path" if format == "html" else "report_json_path"
    report_path = run.get(key)
    if not report_path or not Path(report_path).exists():
        raise HTTPException(status_code=404, detail=f"{format.upper()} report not found")

    media_type = "text/html" if format == "html" else "application/json"
    return FileResponse(
        path=report_path,
        media_type=media_type,
        filename=f"geper_report_{run_id}.{format}",
    )


@app.get(
    "/api/v1/pipeline/{run_id}/log",
    tags=["pipeline"],
    summary="Download the run log",
)
async def download_log(run_id: str, _auth: None = Depends(_require_api_key)) -> Dict:
    """Return captured log lines for a run (if available)."""
    run = _get_run(run_id)
    # Prefer log_path stored in run record (set by _run_pipeline_sync)
    log_path_str = run.get("log_path")
    if log_path_str:
        log_path = Path(log_path_str)
    else:
        # Fallback: derive from output dir and sample_id
        log_path = _OUTPUT_DIR / run["sample_id"] / "pipeline.log"
    if log_path.exists():
        return {"run_id": run_id, "log": log_path.read_text(errors="replace")[-50_000:]}
    return {"run_id": run_id, "log": "", "message": "No log file found"}


def _other_runs_sharing_sample_id(sample_id: str, exclude_run_id: str) -> list:
    """Return run_ids (other than exclude_run_id) that still reference
    sample_id, across both the in-memory cache and the persistent store.

    ARCHITECTURAL BOUNDARY: PipelineRunner keys its work_dir by sample_id
    alone (pipeline/orchestration/runner.py) -- deliberately, so that
    re-running the same sample_id resumes from its checkpoint.json instead
    of starting over. That design choice, made purely for resumability,
    means distinct runs (distinct run_ids) can legitimately share one
    on-disk directory whenever they share a sample_id -- which happens by
    default, since sample_id defaults to the literal "SAMPLE" for any
    caller that doesn't set it.

    DELETE's contract, however, is per-run_id ({run_id} in the route, "Cancel
    or delete a run" in its summary). Nothing in the runner's checkpoint/
    resume design anticipated that contract, and nothing here should ever
    change the runner's directory layout -- this function exists solely to
    keep DELETE from acting past its own boundary. Before rmtree-ing a
    sample_id's work_dir, we must confirm no other run_id still depends on
    it; if one does, the run_id being deleted loses its registry entry but
    the shared directory is left alone for the survivor.
    """
    others = {
        rid for rid, r in _RUNS.items() if rid != exclude_run_id and r.get("sample_id") == sample_id
    }
    for r in _RUN_STORE.list_all(limit=1000):
        rid = r.get("run_id")
        if rid and rid != exclude_run_id and r.get("sample_id") == sample_id:
            others.add(rid)
    return sorted(others)


@app.delete(
    "/api/v1/pipeline/{run_id}",
    tags=["pipeline"],
    summary="Cancel or delete a run",
    status_code=status.HTTP_200_OK,
)
async def delete_run(run_id: str, _auth: None = Depends(_require_api_key)) -> Dict:
    """Cancel a pending/running run or delete artefacts of a finished run.

    For running runs, sends SIGTERM to the pipeline process group,
    waits 5 seconds, then SIGKILL if still alive.
    """
    run = _get_run(run_id)

    if run["status"] in ("pending", "running"):
        # GAP 4: kill the subprocess group
        pgid = _PROCESS_GROUPS.get(run_id)
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
                # Brief wait, then SIGKILL if still alive
                import threading as _threading

                def _sigkill_after():
                    time.sleep(5)
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                _threading.Thread(target=_sigkill_after, daemon=True).start()
            except ProcessLookupError:
                pass
            _PROCESS_GROUPS.pop(run_id, None)

        run["status"] = "cancelled"
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        _RUN_STORE.update(run_id, status="cancelled", finished_at=run["finished_at"])

    # Remove output artefacts -- but only if no other live run still shares
    # this sample_id's work_dir (see _other_runs_sharing_sample_id).
    sample_id = run["sample_id"]
    survivors = _other_runs_sharing_sample_id(sample_id, run_id)
    if survivors:
        logger.warning(
            "Run %s deleted, but work_dir for sample_id=%s preserved: "
            "still referenced by run(s) %s",
            run_id,
            sample_id,
            ", ".join(survivors),
        )
    else:
        work_dir = _OUTPUT_DIR / sample_id
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    _RUN_STORE.delete(run_id)
    _RUNS.pop(run_id, None)
    logger.info("Run deleted: %s", run_id)
    return {"run_id": run_id, "status": "deleted"}


@app.get(
    "/api/v1/pipeline",
    tags=["pipeline"],
    summary="List all runs",
)
async def list_runs(_auth: None = Depends(_require_api_key)) -> Dict:
    """Return a summary list of all known runs (capped at 100)."""
    runs = list(_RUNS.values())[-100:]
    return {
        "total": len(_RUNS),
        "runs": [
            {
                "run_id": r["run_id"],
                "sample_id": r["sample_id"],
                "status": r["status"],
                "progress_pct": r["progress_pct"],
                "started_at": r.get("started_at"),
            }
            for r in runs
        ],
    }


# ─── Checkpoint-based progress refresh ───────────────────────────────────────


def _refresh_progress_from_checkpoint(run_id: str, run: Dict) -> None:
    """Read checkpoint.json to update stage and progress_pct in the run record."""
    import json as _json

    sample_id = run["sample_id"]
    cp_path = _OUTPUT_DIR / sample_id / "checkpoint.json"
    if not cp_path.exists():
        return
    try:
        cp = _json.loads(cp_path.read_text())
        completed = cp.get("completed_stages", [])
        if completed:
            run["stages_completed"] = completed
            last_stage = completed[-1]
            run["stage"] = last_stage
            run["progress_pct"] = _STAGE_PROGRESS.get(last_stage, run["progress_pct"])
    except Exception:
        pass
