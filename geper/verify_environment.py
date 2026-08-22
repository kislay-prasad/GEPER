#!/usr/bin/env python3
"""
GEPER environment / dependency checker.

Run this BEFORE `python main.py ...` (or at the top of a Colab
notebook) to get one human-readable report of exactly what this
runtime can and cannot do, instead of discovering a mismatch three
minutes into a run as a bare traceback from deep inside `transformers`
or `tensorflow`.

Usage:
    python verify_environment.py
    python verify_environment.py --json          # machine-readable
    python verify_environment.py --strict         # exit 1 on any FAIL

This script is READ-ONLY: it never installs, upgrades, or downgrades
anything. `utils/auto_install.py` (used by the pipeline itself) is the
runtime auto-install path; this script's job is purely diagnostic, so
that what it reports is never confused with (and never triggers) any
kind of automatic system-modifying action.

Every check below is independent and best-effort: one check raising an
unexpected exception is caught and reported as its own FAIL/WARN line,
never an uncaught traceback that stops the rest of the report from
being produced.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# --- version-compatibility ground truth ------------------------------------
# Kept in exactly one place (here) and mirrored by the short summary in
# requirements.txt's header comment / README.md's "Compatibility matrix"
# section -- if one changes, the other two should be updated with it.
EXPECTED = {
    "python_min": (3, 11),
    "python_max_exclusive": (3, 13),  # evo2's own PyPI metadata: >=3.11,<3.13
    "torch": "2.7.1",
    "torchvision": "0.22.1",
    "torchaudio": "2.7.1",
    "transformers_min": "5.12.1",
    "transformers_max_exclusive": "6.0.0",
    "accelerate_min": "1.14.0",
    "accelerate_max_exclusive": "2.0.0",
    "tensorflow_min": "2.16.0",
    "tensorflow_max_exclusive": "3.0.0",
    "cuda_min_compute_capability": (8, 0),  # Evo2 / flash-attn floor. T4 (7.5) fails this.
}


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "WARN" | "FAIL" | "SKIP"
    detail: str
    fix: Optional[str] = None


@dataclass
class Report:
    results: List[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, fix: Optional[str] = None) -> None:
        self.results.append(CheckResult(name, status, detail, fix))

    def run(self, name: str, fn: Callable[[], "CheckResult"]) -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - a check itself must never crash the report
            result = CheckResult(name, "WARN", f"Check raised an unexpected error: {exc!r}", fix=None)
        self.results.append(result)

    def has_failures(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "results": [r.__dict__ for r in self.results],
            "overall": "FAIL" if self.has_failures() else "PASS",
        }


def _parse_version(v: str) -> tuple:
    """Best-effort dotted-version parse, ignoring any pre/post/dev suffix."""
    parts = []
    for chunk in v.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _in_range(v: str, min_v: Optional[str], max_v_exclusive: Optional[str]) -> bool:
    parsed = _parse_version(v)
    if min_v and parsed < _parse_version(min_v):
        return False
    if max_v_exclusive and parsed >= _parse_version(max_v_exclusive):
        return False
    return True


def _is_importable(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python() -> CheckResult:
    current = sys.version_info[:2]
    lo, hi = EXPECTED["python_min"], EXPECTED["python_max_exclusive"]
    if lo <= current < hi:
        return CheckResult(
            "Python", "PASS", f"Python {platform.python_version()} ({lo[0]}.{lo[1]}-{hi[0]}.{hi[1] - 1} required)."
        )
    return CheckResult(
        "Python",
        "FAIL",
        f"Python {platform.python_version()} detected; Bij AI requires "
        f"{lo[0]}.{lo[1]} <= python < {hi[0]}.{hi[1]} (evo2's own published "
        f"metadata is the binding constraint; torch/transformers/rna-fm "
        f"all separately accept a wider range).",
        fix=f"Install Python {lo[0]}.{lo[1]} or {lo[0]}.{hi[1] - 1} (e.g. via pyenv or "
        f"a fresh Colab runtime) and re-create your virtualenv.",
    )


def check_torch() -> CheckResult:
    if not _is_importable("torch"):
        return CheckResult(
            "torch",
            "FAIL",
            "torch is not installed.",
            fix=f"pip install torch=={EXPECTED['torch']} "
            f"(CUDA build: pip install torch=={EXPECTED['torch']} "
            f"--index-url https://download.pytorch.org/whl/cu128)",
        )
    import torch  # noqa: WPS433 - intentional lazy import, only after existence check

    version = torch.__version__.split("+")[0]
    if version == EXPECTED["torch"]:
        status, detail = "PASS", f"torch {torch.__version__} detected (matches pinned {EXPECTED['torch']})."
    else:
        status = "WARN"
        detail = (
            f"torch {torch.__version__} detected; Bij AI is pinned/tested against "
            f"exactly {EXPECTED['torch']} because Evo2's flash-attn dependency is "
            f"compiled from source against a specific torch+CUDA ABI. A different "
            f"torch version may still work for HyenaDNA/RNA-FM/ESM2/"
            f"AlphaMissense/MMSplice (none of those need flash-attn), but is "
            f"unverified."
        )
    return CheckResult(
        "torch",
        status,
        detail,
        fix=None if status == "PASS" else f"pip install torch=={EXPECTED['torch']} --force-reinstall",
    )


def check_torchvision() -> CheckResult:
    if not _is_importable("torchvision"):
        return CheckResult(
            "torchvision",
            "WARN",
            "torchvision is not installed. Only required for HyenaDNA's "
            "StochasticDepth; RNA-FM/ESM2/AlphaMissense/MMSplice do not need it.",
            fix=f"pip install torchvision=={EXPECTED['torchvision']}",
        )
    import torchvision  # noqa: WPS433

    if torchvision.__version__.split("+")[0] == EXPECTED["torchvision"]:
        return CheckResult("torchvision", "PASS", f"torchvision {torchvision.__version__} detected.")
    return CheckResult(
        "torchvision",
        "WARN",
        f"torchvision {torchvision.__version__} detected; Bij AI pins "
        f"{EXPECTED['torchvision']} (PyTorch's own compatibility matrix pairing "
        f"for torch {EXPECTED['torch']}). A mismatched torch/torchvision pair is "
        f"the single most common cause of a cryptic 'undefined symbol' import "
        f"error at startup.",
        fix=f"pip install torchvision=={EXPECTED['torchvision']}",
    )


def check_torchaudio() -> CheckResult:
    """
    torchaudio is not imported anywhere in GEPER's own code, but is
    checked anyway: an unpinned or mismatched torchaudio build (its
    compiled extension registers custom ops against torch's C++ API at
    import time via TORCH_LIBRARY_IMPL) fails with `OSError: undefined
    symbol: torch_library_impl` the moment ANYTHING in the process
    imports it -- and because that failure isn't scoped to whichever
    module triggered the import, it can surface while loading an
    unrelated model (e.g. ESM2) if a transitive dependency
    (rna-fm, evo2/vtx) or the hosting environment (Colab ships
    its own pre-installed torchaudio tied to Colab's default torch
    version, not necessarily this project's pinned one) pulls in a
    torchaudio build that doesn't match torch==2.7.1's ABI.
    """
    if not _is_importable("torchaudio"):
        return CheckResult(
            "torchaudio",
            "WARN",
            "torchaudio is not installed. Bij AI itself never imports it, but leaving "
            "it absent/unpinned in a shared environment (e.g. Colab, which ships its "
            "own pre-installed torchaudio build) is exactly how a mismatched version "
            "gets pulled in later and crashes an unrelated import "
            "(`OSError: undefined symbol: torch_library_impl`) -- pin it explicitly.",
            fix=f"pip install torchaudio=={EXPECTED['torchaudio']}",
        )
    import torchaudio  # noqa: WPS433

    if torchaudio.__version__.split("+")[0] == EXPECTED["torchaudio"]:
        return CheckResult("torchaudio", "PASS", f"torchaudio {torchaudio.__version__} detected.")
    return CheckResult(
        "torchaudio",
        "FAIL",
        f"torchaudio {torchaudio.__version__} detected; Bij AI pins "
        f"{EXPECTED['torchaudio']} (PyTorch's own compatibility matrix pairing for "
        f"torch {EXPECTED['torch']}). A mismatched torch/torchaudio pair fails with "
        f"`OSError: undefined symbol: torch_library_impl` the moment torchaudio is "
        f"imported by ANYTHING in the process -- this can appear to come from an "
        f"unrelated model import (e.g. ESM2) if a transitive dependency imports "
        f"torchaudio first.",
        fix=f"pip install torchaudio=={EXPECTED['torchaudio']} --force-reinstall",
    )


def check_transformers() -> CheckResult:
    if not _is_importable("transformers"):
        return CheckResult(
            "transformers",
            "FAIL",
            "transformers is not installed.",
            fix=f"pip install 'transformers>={EXPECTED['transformers_min']},<{EXPECTED['transformers_max_exclusive']}'",
        )
    import transformers  # noqa: WPS433

    if _in_range(transformers.__version__, EXPECTED["transformers_min"], EXPECTED["transformers_max_exclusive"]):
        return CheckResult("transformers", "PASS", f"transformers {transformers.__version__} detected.")
    return CheckResult(
        "transformers",
        "FAIL",
        f"transformers {transformers.__version__} detected; Bij AI requires "
        f">={EXPECTED['transformers_min']},<{EXPECTED['transformers_max_exclusive']}.",
        fix=f"pip install 'transformers>={EXPECTED['transformers_min']},<{EXPECTED['transformers_max_exclusive']}'",
    )


def check_accelerate() -> CheckResult:
    if not _is_importable("accelerate"):
        return CheckResult(
            "accelerate",
            "FAIL",
            "accelerate is not installed.",
            fix=f"pip install 'accelerate>={EXPECTED['accelerate_min']},<{EXPECTED['accelerate_max_exclusive']}'",
        )
    import accelerate  # noqa: WPS433

    if _in_range(accelerate.__version__, EXPECTED["accelerate_min"], EXPECTED["accelerate_max_exclusive"]):
        return CheckResult("accelerate", "PASS", f"accelerate {accelerate.__version__} detected.")
    return CheckResult(
        "accelerate",
        "WARN",
        f"accelerate {accelerate.__version__} detected; Bij AI requires "
        f">={EXPECTED['accelerate_min']},<{EXPECTED['accelerate_max_exclusive']}.",
        fix=f"pip install 'accelerate>={EXPECTED['accelerate_min']},<{EXPECTED['accelerate_max_exclusive']}'",
    )


def check_tensorflow() -> CheckResult:
    if not _is_importable("tensorflow"):
        return CheckResult(
            "tensorflow",
            "WARN",
            "tensorflow is not installed; MMSplice (Keras-based) will be "
            "unavailable and skipped gracefully. Every other model is unaffected.",
            fix=f"pip install 'tensorflow>={EXPECTED['tensorflow_min']},<{EXPECTED['tensorflow_max_exclusive']}'",
        )
    import tensorflow as tf  # noqa: WPS433

    if _in_range(tf.__version__, EXPECTED["tensorflow_min"], EXPECTED["tensorflow_max_exclusive"]):
        return CheckResult("tensorflow", "PASS", f"tensorflow {tf.__version__} detected.")
    return CheckResult(
        "tensorflow",
        "WARN",
        f"tensorflow {tf.__version__} detected; Bij AI requires "
        f">={EXPECTED['tensorflow_min']},<{EXPECTED['tensorflow_max_exclusive']}.",
        fix=f"pip install 'tensorflow>={EXPECTED['tensorflow_min']},<{EXPECTED['tensorflow_max_exclusive']}'",
    )


def check_mmsplice_availability() -> CheckResult:
    """
    GEPER never `pip install`s plain `mmsplice` (see requirements.txt /
    MMSpliceConfig docstring) -- it obtains the package's bundled .h5
    weight files via `--no-deps` at runtime. This check only confirms
    tensorflow is present (mmsplice's own package presence is optional
    and auto-provisioned; its absence here is not itself a failure).
    """
    if not _is_importable("tensorflow"):
        return CheckResult(
            "MMSplice prerequisites",
            "WARN",
            "tensorflow missing -> MMSplice cannot run (see 'tensorflow' check above).",
        )
    installed = _is_importable("mmsplice")
    return CheckResult(
        "MMSplice prerequisites",
        "PASS",
        f"tensorflow available; `mmsplice` package "
        f"{'already present' if installed else 'not yet installed (will be auto-provisioned --no-deps on first use)'}.",
    )


def check_cuda_gpu() -> CheckResult:
    if not _is_importable("torch"):
        return CheckResult("CUDA / GPU", "SKIP", "torch not installed; cannot query GPU state.")
    import torch  # noqa: WPS433

    if not torch.cuda.is_available():
        return CheckResult(
            "CUDA / GPU",
            "WARN",
            "No CUDA GPU detected. CPU fallback works for HyenaDNA/"
            "RNA-FM/ESM2/AlphaMissense/MMSplice (slower); Evo2 has no practical "
            "CPU path and is skipped entirely.",
        )
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    mem_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
    min_cap = EXPECTED["cuda_min_compute_capability"]
    detail = f"GPU '{name}' detected, compute capability {cap[0]}.{cap[1]}, {mem_gb:.1f} GB VRAM."
    if cap >= min_cap:
        return CheckResult("CUDA / GPU", "PASS", detail)
    return CheckResult(
        "CUDA / GPU",
        "WARN",
        detail + f" This is below compute capability {min_cap[0]}.{min_cap[1]}, so "
        f"Evo2/flash-attn is unavailable here (this is expected and by design on "
        f"e.g. Tesla T4 -- see EVO2_T4_HARDWARE_FINDINGS.md; Evo2 is intentionally "
        f"disabled rather than crashing). Every other model still runs on this GPU.",
    )


def check_internet() -> CheckResult:
    hosts = [
        ("huggingface.co", 443),
        ("rest.ensembl.org", 443),
        ("eutils.ncbi.nlm.nih.gov", 443),
    ]
    reachable = []
    unreachable = []
    for host, port in hosts:
        try:
            socket.create_connection((host, port), timeout=4).close()
            reachable.append(host)
        except OSError:
            unreachable.append(host)
    if not unreachable:
        return CheckResult("Internet connectivity", "PASS", f"Reached: {', '.join(reachable)}.")
    if reachable:
        return CheckResult(
            "Internet connectivity",
            "WARN",
            f"Reached {reachable}, could not reach {unreachable}. Model downloads "
            f"and/or ClinVar/dbSNP/Ensembl/gnomAD lookups touching an unreachable "
            f"host will fail gracefully (logged per-variant) rather than crash the run.",
        )
    return CheckResult(
        "Internet connectivity",
        "WARN",
        f"Could not reach any of {[h for h, _ in hosts]}. First-run model downloads "
        f"will fail; ClinVar/dbSNP/Ensembl/gnomAD (GraphQL fallback) lookups will "
        f"all fail gracefully per-variant. AI-only, fully-offline runs with "
        f"pre-cached models and local databases (AlphaMissense tabix file, gnomAD "
        f"local indexed VCF) are unaffected.",
    )


def check_blast() -> CheckResult:
    """
    Verifies all three local BLAST+ command line tools GEPER can use
    (blastn for search, makeblastdb for auto-building a database from
    a FASTA reference, blastdbcmd for inspecting/validating a
    database), reporting each tool's version string. This is the
    "automatic startup validation" for the local BLAST+ backend --
    read-only, like every other check in this script; it never
    installs or builds anything itself (that happens lazily, in
    BLASTClient, only when a run actually needs it).

    Local BLAST+ is the preferred backend when available (see
    database/blast_client.py); missing binaries are a WARN, not a
    FAIL, because GEPER falls back to remote NCBI BLAST (or skips
    BLAST gracefully) rather than failing the whole run.
    """
    from database.blast_client import get_blast_tool_versions

    versions = get_blast_tool_versions()
    found = {tool: v for tool, v in versions.items() if v is not None}
    missing = [tool for tool, v in versions.items() if v is None]

    version_lines = "; ".join(f"{tool}: {v}" for tool, v in found.items())

    if not missing:
        return CheckResult(
            "BLAST+ (blastn/makeblastdb/blastdbcmd)",
            "PASS",
            f"All local BLAST+ command line tools found -- {version_lines}.",
        )
    if found:
        return CheckResult(
            "BLAST+ (blastn/makeblastdb/blastdbcmd)",
            "WARN",
            f"Found: {version_lines}. Missing from PATH: {', '.join(missing)}. "
            "Local BLAST features requiring a missing tool are unavailable "
            "(e.g. no blastn means no local search; no makeblastdb means no "
            "FASTA auto-build); remote NCBI BLAST is still used as a "
            "fallback unless GEPER_AI_ONLY=true.",
            fix="apt-get install ncbi-blast+  (or: conda install -c bioconda blast)",
        )
    return CheckResult(
        "BLAST+ (blastn/makeblastdb/blastdbcmd)",
        "WARN",
        "No local BLAST+ command line tools found on PATH. Remote NCBI "
        "BLAST is used instead (slower, subject to NCBI's shared queue) "
        "unless GEPER_AI_ONLY=true, or BLAST is skipped gracefully if "
        "remote is also unavailable (e.g. Biopython can't be installed).",
        fix="apt-get install ncbi-blast+  (or: conda install -c bioconda blast)",
    )


def check_blast_database() -> CheckResult:
    """
    Reports what GEPER_BLAST_MODE currently resolves to, and whether
    a usable local BLAST database is detected at the configured path
    (GEPER_BLAST_DATABASE / GEPER_BLAST_LOCAL_DB / BLASTDB), without
    constructing a full BLASTClient (which would also touch Biopython
    auto-install and disk-cache setup -- more than a read-only
    diagnostic should trigger).
    """
    from database.blast_client import BLASTClient, _has_local_db_files
    from config import CONFIG

    configured_mode = CONFIG.api.BLAST_MODE
    db_path = CONFIG.api.BLAST_LOCAL_DB_PATH or None
    reference_fasta = CONFIG.api.BLAST_REFERENCE_FASTA or None

    if configured_mode not in ("auto", "local", "remote"):
        return CheckResult(
            "BLAST database / mode",
            "WARN",
            f"GEPER_BLAST_MODE='{configured_mode}' is not one of "
            "auto/local/remote; BLASTClient will reject this at "
            "construction time.",
            fix="Set GEPER_BLAST_MODE to 'auto', 'local', or 'remote'.",
        )

    if not db_path:
        detail = (
            f"GEPER_BLAST_MODE='{configured_mode}'. No local BLAST database "
            "configured (GEPER_BLAST_DATABASE / GEPER_BLAST_LOCAL_DB / "
            "BLASTDB are all unset)."
        )
        if reference_fasta:
            detail += (
                f" GEPER_BLAST_REFERENCE_FASTA='{reference_fasta}' is set, "
                "so a database will be auto-built there with makeblastdb the "
                "first time BLAST runs."
            )
        return CheckResult("BLAST database / mode", "WARN" if configured_mode == "local" else "PASS", detail)

    has_db = _has_local_db_files(db_path)
    resolved = BLASTClient._resolve_auto_mode(db_path)
    if has_db:
        return CheckResult(
            "BLAST database / mode",
            "PASS",
            f"GEPER_BLAST_MODE='{configured_mode}'; local database detected "
            f"at '{db_path}' (auto-resolution -> '{resolved}').",
        )
    detail = (
        f"GEPER_BLAST_MODE='{configured_mode}'; GEPER_BLAST_DATABASE is set "
        f"to '{db_path}' but no database files were found there."
    )
    if reference_fasta:
        detail += (
            f" GEPER_BLAST_REFERENCE_FASTA='{reference_fasta}' is also set, "
            "so a database will be auto-built at this path via makeblastdb "
            "the first time BLAST runs."
        )
        return CheckResult("BLAST database / mode", "PASS", detail)
    status = "WARN" if configured_mode in ("auto", "local") else "PASS"
    return CheckResult(
        "BLAST database / mode",
        status,
        detail,
        fix=(
            "Point GEPER_BLAST_DATABASE at an existing makeblastdb database, "
            "or set GEPER_BLAST_REFERENCE_FASTA to auto-build one."
        )
        if status == "WARN"
        else None,
    )


def check_tabix() -> CheckResult:
    binary = shutil.which("tabix")
    if binary:
        return CheckResult("tabix (htslib)", "PASS", f"Found tabix at '{binary}'.")
    return CheckResult(
        "tabix (htslib)",
        "WARN",
        "No 'tabix' binary on PATH. AlphaMissense and any locally-indexed "
        "gnomAD catalogue lookups are unavailable and will be skipped gracefully "
        "until this is installed (Bij AI auto-installs it via apt-get on Debian/"
        "Ubuntu the first time it's needed).",
        fix="apt-get install tabix  (or: conda install -c bioconda htslib)",
    )


def check_ram() -> CheckResult:
    try:
        with open("/proc/meminfo") as fh:
            meminfo = fh.read()
        total_kb = int([ln for ln in meminfo.splitlines() if ln.startswith("MemTotal:")][0].split()[1])
        avail_kb = int([ln for ln in meminfo.splitlines() if ln.startswith("MemAvailable:")][0].split()[1])
        total_gb, avail_gb = total_kb / 1e6, avail_kb / 1e6
    except (FileNotFoundError, IndexError, ValueError):
        return CheckResult("RAM", "SKIP", "Could not read /proc/meminfo on this platform (non-Linux?).")
    status = "PASS" if avail_gb >= 4 else "WARN"
    detail = f"{avail_gb:.1f} GB available / {total_gb:.1f} GB total."
    if status == "WARN":
        detail += " ESM-2 (650M params) and MMSplice's TensorFlow submodels may be tight on <4GB available RAM."
    return CheckResult("RAM", status, detail)


def check_disk() -> CheckResult:
    usage = shutil.disk_usage(os.getcwd())
    free_gb = usage.free / (1024**3)
    status = "PASS" if free_gb >= 15 else "WARN"
    detail = f"{free_gb:.1f} GB free on the filesystem containing {os.getcwd()}."
    if status == "WARN":
        detail += (
            " Model checkpoints (RNA-FM/ESM2 via HuggingFace cache, "
            "HyenaDNA's checkpoint, MMSplice's .h5 weights, and an optional "
            "AlphaMissense/gnomAD tabix catalogue download) can together need "
            "10-20+ GB of disk."
        )
    return CheckResult("Disk space", status, detail)


def check_installed_models() -> CheckResult:
    """
    Statically reports which model wrapper modules import cleanly in
    THIS process (i.e. their own top-level `import torch` /
    `from transformers import ...` etc. succeed) -- this is a real,
    executed check, not a guess. It does NOT download or load any
    checkpoint weights (that only happens on first inference, lazily),
    so 'imports OK' is a necessary but not sufficient condition for
    'will actually run a forward pass successfully' -- see the
    PHASE1_VERIFICATION_REPORT.md note on what this script can and
    cannot prove without a live GPU/network.
    """
    module_map = {
        "HyenaDNA": "models.hyenadna",
        "RNA-FM": "models.rna_fm",
        "ESM2": "models.esm2",
        "AlphaMissense": "models.alphamissense",
        "MMSplice": "pipeline.models.mmsplice.loader",
        "Enformer": "pipeline.models.enformer_plugin",
        "Borzoi": "pipeline.models.borzoi_plugin",
    }
    ok, failed = [], {}
    for label, module_name in module_map.items():
        try:
            importlib.import_module(module_name)
            ok.append(label)
        except Exception as exc:  # noqa: BLE001
            failed[label] = str(exc)[:200]

    if not failed:
        return CheckResult(
            "Model module imports",
            "PASS",
            f"All {len(module_map)} model wrapper modules import cleanly: {', '.join(ok)}. "
            f"(Import success only -- see note on live load/inference verification.)",
        )
    detail = f"OK: {', '.join(ok) or 'none'}. Failed to import: " + "; ".join(
        f"{name} ({err})" for name, err in failed.items()
    )
    return CheckResult("Model module imports", "FAIL", detail)


def check_installed_databases() -> CheckResult:
    module_map = {
        "ClinVar": "database.clinvar_client",
        "dbSNP": "database.dbsnp_client",
        "BLAST": "database.blast_client",
        "gnomAD": "pipeline.gnomad.lookup",
        "ClinGen": "pipeline.clingen.lookup",
        "UniProt": "pipeline.uniprot.lookup",
        "InterPro/Pfam": "pipeline.interpro.lookup",
        "AlphaFold DB": "pipeline.alphafold.lookup",
    }
    ok, failed = [], {}
    for label, module_name in module_map.items():
        try:
            importlib.import_module(module_name)
            ok.append(label)
        except Exception as exc:  # noqa: BLE001
            failed[label] = str(exc)[:200]
    if not failed:
        return CheckResult("Database client imports", "PASS", f"All import cleanly: {', '.join(ok)}.")
    detail = f"OK: {', '.join(ok) or 'none'}. Failed: " + "; ".join(f"{n} ({e})" for n, e in failed.items())
    return CheckResult("Database client imports", "FAIL", detail)


def check_biological_evidence_layer() -> CheckResult:
    """
    Startup validation for the UniProt / InterPro-Pfam / AlphaFold DB
    biological evidence layer: confirms each integration's `Lookup`
    facade constructs cleanly (config parses, cache initializes,
    provider wiring is intact) without making any network call --
    exactly the same "prove the plumbing without requiring network
    access this sandbox doesn't have" shape as
    `check_installed_databases` above. A construction failure here
    would mean a broken deployment before a single variant is ever
    processed; each integration's own graceful-fallback behavior
    (`OFFLINE_MODE`, `ENABLED=false`, provider-level try/except) is
    covered separately in `tests/test_uniprot_*.py` /
    `tests/test_interpro_*.py` / `tests/test_alphafold_*.py`, not here.
    """
    from config import CONFIG

    checks = [
        ("UniProt", CONFIG.uniprot.ENABLED, "pipeline.uniprot.lookup", "UniProtLookup"),
        ("InterPro/Pfam", CONFIG.interpro.ENABLED, "pipeline.interpro.lookup", "InterProLookup"),
        ("AlphaFold DB", CONFIG.alphafold.ENABLED, "pipeline.alphafold.lookup", "AlphaFoldLookup"),
    ]
    ok, disabled, failed = [], [], {}
    for label, enabled, module_name, class_name in checks:
        if not enabled:
            disabled.append(label)
            continue
        try:
            module = importlib.import_module(module_name)
            getattr(module, class_name)()  # construct: config + cache + provider wiring, no network call
            ok.append(label)
        except Exception as exc:  # noqa: BLE001
            failed[label] = str(exc)[:200]

    if failed:
        detail = (
            f"OK: {', '.join(ok) or 'none'}. Disabled: {', '.join(disabled) or 'none'}. "
            f"Failed to construct: " + "; ".join(f"{n} ({e})" for n, e in failed.items())
        )
        return CheckResult("Biological evidence layer startup validation", "FAIL", detail)

    detail = f"OK: {', '.join(ok) or 'none'}."
    if disabled:
        detail += f" Disabled via config: {', '.join(disabled)}."
    return CheckResult("Biological evidence layer startup validation", "PASS", detail)


def check_dependency_compatibility() -> CheckResult:
    """
    Cross-package checks that a plain 'is torch installed' check can't
    catch: e.g. a torch/torchvision pair from two different releases,
    which imports fine on its own but explodes the moment torchvision
    is actually used (the classic 'undefined symbol' failure this
    project's requirements.txt / DEPENDENCY_MIGRATION_REPORT.md
    document at length).
    """
    problems = []
    if _is_importable("torch") and _is_importable("torchvision"):
        import torch  # noqa: WPS433
        import torchvision  # noqa: WPS433

        t_version = torch.__version__.split("+")[0]
        tv_version = torchvision.__version__.split("+")[0]
        if t_version != EXPECTED["torch"] or tv_version != EXPECTED["torchvision"]:
            problems.append(
                f"torch=={t_version} / torchvision=={tv_version} is not the "
                f"tested pair (torch=={EXPECTED['torch']} / "
                f"torchvision=={EXPECTED['torchvision']}); an ABI mismatch here "
                f"is the single most common source of a bare 'undefined symbol' "
                f"traceback at import time."
            )
    if _is_importable("torch") and _is_importable("torchaudio"):
        import torch  # noqa: WPS433
        import torchaudio  # noqa: WPS433

        t_version = torch.__version__.split("+")[0]
        ta_version = torchaudio.__version__.split("+")[0]
        if t_version != EXPECTED["torch"] or ta_version != EXPECTED["torchaudio"]:
            problems.append(
                f"torch=={t_version} / torchaudio=={ta_version} is not the "
                f"tested pair (torch=={EXPECTED['torch']} / "
                f"torchaudio=={EXPECTED['torchaudio']}); this exact mismatch is "
                f"what produces `OSError: undefined symbol: torch_library_impl` "
                f"the moment torchaudio is imported by anything in the process "
                f"(directly, or transitively via rna-fm/evo2's own "
                f"dependencies, or a pre-installed Colab build) -- it can appear "
                f"to originate from an unrelated model import (e.g. ESM2) rather "
                f"than from torchaudio itself."
            )
    if not problems:
        return CheckResult("Cross-package compatibility", "PASS", "No known-bad combination detected.")
    return CheckResult("Cross-package compatibility", "WARN", " ".join(problems))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_CHECKS: List[Callable[[], CheckResult]] = [
    check_python,
    check_torch,
    check_torchvision,
    check_torchaudio,
    check_transformers,
    check_accelerate,
    check_tensorflow,
    check_mmsplice_availability,
    check_cuda_gpu,
    check_dependency_compatibility,
    check_internet,
    check_blast,
    check_blast_database,
    check_tabix,
    check_ram,
    check_disk,
    check_installed_models,
    check_installed_databases,
    check_biological_evidence_layer,
]

_STATUS_ICON = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "SKIP": "⏭️ "}


def run_all() -> Report:
    report = Report()
    for check_fn in _ALL_CHECKS:
        report.run(check_fn.__name__, check_fn)
    return report


def print_human(report: Report) -> None:
    print("=" * 78)
    print("Bij AI Environment Verification")
    print("=" * 78)
    for r in report.results:
        icon = _STATUS_ICON.get(r.status, "?")
        print(f"{icon} [{r.status:4s}] {r.name}")
        print(f"        {r.detail}")
        if r.fix:
            print(f"        -> Fix: {r.fix}")
        print()
    print("-" * 78)
    n_fail = sum(1 for r in report.results if r.status == "FAIL")
    n_warn = sum(1 for r in report.results if r.status == "WARN")
    n_pass = sum(1 for r in report.results if r.status == "PASS")
    print(f"Summary: {n_pass} passed, {n_warn} warnings, {n_fail} failures.")
    if n_fail:
        print("One or more required dependencies are missing or incompatible. See the '-> Fix' lines above.")
    else:
        print(
            "No blocking issues detected. Warnings (if any) describe optional "
            "features that will be skipped gracefully, not blocking failures."
        )
    print("=" * 78)


def main() -> int:
    strict = "--strict" in sys.argv
    as_json = "--json" in sys.argv

    report = run_all()

    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_human(report)

    if strict and report.has_failures():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
