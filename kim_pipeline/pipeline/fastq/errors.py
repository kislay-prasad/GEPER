"""
pipeline/fastq/errors.py
─────────────────────────
Shared exception types for the GEPER genomic pipeline.

``FastqPipelineError`` is the single error type raised by every stage
and utility module in ``pipeline/``. Callers catch this class and can
inspect ``stage`` and ``tool`` to give the user precise remediation
instructions.

This module also houses ``_require`` and ``_run`` — the two low-level
subprocess primitives used identically across alignment, variant-calling,
annotation, and reporting stages. Centralising them here means:

* No stage imports these from another stage's module (avoiding
  accidental circular imports across ``pipeline/`` sub-packages).
* Any improvement to error formatting benefits every stage at once.

The original codebase imported ``FastqPipelineError``, ``_require``, and
``_run`` from ``geper.pipeline.fastq.pipeline``; that full-repo symbol
continues to resolve because this module is importable as either
``pipeline.fastq.errors`` (local-package import) or — when the repo root
is on ``sys.path`` — can be re-exported from the legacy location if
needed.  Stage modules in this extract already use the canonical path
``pipeline.fastq.errors``; the compatibility shim at
``pipeline/fastq/pipeline_compat.py`` covers the ``geper.*`` import
path used by the existing stage files.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from pipeline.utils.process_control import kill_process_tree_now, spawn_tracked

logger = logging.getLogger("geper.pipeline")

# Default wall-clock ceiling for any subprocess run through _run()
# below (bwa, samtools, bcftools, freebayes, ...). Previously
# subprocess.run() here had no timeout at all, so a stalled or
# pathologically slow external tool call could hang a pipeline run
# indefinitely with no way to recover. 60 minutes comfortably covers
# every stage observed in practice (variant calling on a small test
# BAM has taken up to ~17 minutes) while still bounding a genuine
# hang. Override per-call via _run()'s timeout_seconds parameter for
# a stage/input that legitimately needs more.
_DEFAULT_TIMEOUT_SECONDS = 60 * 60  # 60 minutes


# ─── Exception ────────────────────────────────────────────────────────────────


class FastqPipelineError(Exception):
    """Raised by any pipeline stage when a non-recoverable error occurs.

    Attributes:
        message:  Human-readable description of what went wrong.
        stage:    Dotted stage identifier, e.g. ``"alignment.bwa"``.
        tool:     Optional name of the missing / failing external tool.
    """

    def __init__(
        self,
        message: str,
        stage: str = "",
        tool: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.tool = tool

    def __str__(self) -> str:  # noqa: D105
        parts = [self.message]
        if self.stage:
            parts.append(f"[stage={self.stage}]")
        if self.tool:
            parts.append(f"[tool={self.tool}]")
        return " ".join(parts)


# ─── Subprocess helpers ───────────────────────────────────────────────────────


@dataclass
class _RunResult:
    stdout: str
    stderr: str
    returncode: int


def _require(tool: str, stage: str) -> str:
    """Return the absolute path of *tool* on PATH, or raise
    ``FastqPipelineError`` with an actionable install suggestion.

    Args:
        tool:   Executable name (e.g. ``"samtools"``).
        stage:  Dotted stage identifier for the error message.

    Returns:
        Absolute path string as returned by ``shutil.which``.
    """
    path = shutil.which(tool)
    if path is None:
        raise FastqPipelineError(
            f"Required tool '{tool}' is not installed or not on PATH. "
            f"Install it (e.g. 'apt install {tool}' or "
            f"'conda install -c bioconda {tool}') and re-run.",
            stage=stage,
            tool=tool,
        )
    return path


def _run(
    cmd: List[str],
    stage: str,
    capture_output: bool = True,
    input_text: Optional[str] = None,
    timeout_seconds: Optional[float] = _DEFAULT_TIMEOUT_SECONDS,
) -> _RunResult:
    """Run *cmd* as a subprocess and return a ``_RunResult``.

    Raises:
        FastqPipelineError: if the process exits with a non-zero code,
            or if it does not finish within *timeout_seconds*.

    Args:
        cmd:             Argument list, same as ``subprocess.run``.
        stage:           Dotted stage id for error attribution.
        capture_output:  Whether to capture stdout/stderr (default True).
        input_text:      Optional text piped to the process stdin.
        timeout_seconds: Maximum wall-clock time (seconds) to let the
            subprocess run before it is killed and a
            ``FastqPipelineError`` is raised. Defaults to
            ``_DEFAULT_TIMEOUT_SECONDS`` (60 minutes); pass a larger
            value for a stage/input that legitimately needs more time,
            or ``None`` to wait indefinitely (the previous, unbounded
            behavior). Every existing call site keeps working
            unchanged and now gets the 60-minute default automatically.

    Spawns via ``spawn_tracked`` (not a bare ``subprocess.run``) so a run in
    progress can be found and killed by ``DELETE /api/v1/pipeline/{run_id}``
    -- see ``pipeline/utils/process_control.py``. External behavior
    (exceptions raised, ``_RunResult`` shape) is unchanged from before.
    """
    try:
        proc = spawn_tracked(
            cmd,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            stdin=subprocess.PIPE if input_text is not None else None,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FastqPipelineError(
            f"Executable not found when running {cmd[0]!r}: {exc}",
            stage=stage,
            tool=cmd[0],
        ) from exc

    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree_now(proc)
        try:
            proc.communicate(timeout=1)  # drain pipes / reap after kill, best-effort
        except Exception:
            pass
        raise FastqPipelineError(
            f"Command {' '.join(cmd)!r} timed out after {timeout_seconds:.0f}s and was killed.",
            stage=stage,
            tool=cmd[0],
        ) from exc

    if proc.returncode != 0:
        raise FastqPipelineError(
            f"Command {' '.join(cmd[:3])!r} failed (exit {proc.returncode}).\n"
            f"stderr: {(stderr or '').strip()[:2000]}",
            stage=stage,
            tool=cmd[0],
        )

    return _RunResult(
        stdout=stdout or "",
        stderr=stderr or "",
        returncode=proc.returncode,
    )
