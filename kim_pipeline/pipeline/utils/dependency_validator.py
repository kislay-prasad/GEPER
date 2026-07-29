"""
pipeline/utils/dependency_validator.py
────────────────────────────────────────
Startup dependency validation for GEPER.

This module is intentionally standalone: it does not import or modify any
pipeline stage, ``PipelineRunner``, or any AI model integration.  It only
performs read-only discovery of external binaries required for the pipeline
to run (bwa, minimap2, samtools, freebayes, bcftools) plus the optional
VEP annotator.

Design goals
────────────
1. Discover all required external tools **once**, up front, instead of
   letting each stage fail independently mid-run.
2. Produce a single, clear, consolidated error if any *required* tool is
   missing — the caller should fail fast, before Stage 1 of the pipeline
   ever starts.
3. Treat optional tools (currently: VEP) as warnings only — never fatal.
4. Be fully unit-testable: all subprocess/shutil calls are isolated behind
   small functions that tests can monkeypatch.

Nothing in this module is imported by ``pipeline/orchestration/runner.py``;
it is wired in at the CLI layer (``main.py``) so that ``PipelineRunner``
itself remains untouched.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ─── Tool registry ─────────────────────────────────────────────────────────────
# name          -> (candidate executable names, version_args, required?)
# Some tools have alternate/interchangeable binaries (e.g. bwa vs bwa-mem2);
# any one of the candidates being present satisfies the requirement.

@dataclass(frozen=True)
class ToolSpec:
    name: str
    candidates: tuple
    version_args: tuple = ("--version",)
    required: bool = True
    notes: str = ""


TOOL_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="bwa",
        candidates=("bwa-mem2", "bwa"),
        version_args=(),  # bwa prints version to stderr with no args
        required=True,
        notes="Short-read aligner (BWA-MEM or BWA-MEM2).",
    ),
    ToolSpec(
        name="minimap2",
        candidates=("minimap2",),
        version_args=("--version",),
        required=False,
        notes="Long-read / alternate aligner. Only required if bwa is unavailable "
              "and minimap2 is selected as the aligner.",
    ),
    ToolSpec(
        name="samtools",
        candidates=("samtools",),
        version_args=("--version",),
        required=True,
        notes="BAM sorting, indexing, and alignment metrics.",
    ),
    ToolSpec(
        name="freebayes",
        candidates=("freebayes",),
        version_args=("--version",),
        required=True,
        notes="Variant calling.",
    ),
    ToolSpec(
        name="bcftools",
        candidates=("bcftools",),
        version_args=("--version",),
        required=True,
        notes="VCF normalization and filtering.",
    ),
    ToolSpec(
        name="vep",
        candidates=("vep",),
        version_args=("--help",),
        required=False,
        notes="Optional external annotator. GEPER degrades gracefully without it.",
    ),
]

# NOTE on "required": bwa and minimap2 are alternate aligners. The pipeline's
# aligner selection is config-driven ("auto" tries bwa first, falls back to
# minimap2). We mark bwa as required (the default path) and minimap2 as
# optional, but if a config explicitly requests minimap2, the caller of
# `validate_dependencies()` can pass `require_minimap2=True` to flip that.


@dataclass
class ToolCheckResult:
    name: str
    found: bool
    version: Optional[str]
    required: bool
    status: str  # "OK" | "MISSING" | "WARNING"
    path: Optional[str] = None
    notes: str = ""


@dataclass
class DependencyReport:
    results: List[ToolCheckResult] = field(default_factory=list)

    @property
    def missing_required(self) -> List[ToolCheckResult]:
        return [r for r in self.results if r.required and not r.found]

    @property
    def ok(self) -> bool:
        return len(self.missing_required) == 0

    def as_table(self) -> str:
        """Render a plain-text table: Tool | Found? | Version | Required? | Status."""
        headers = ("Tool", "Found?", "Version", "Required?", "Status")
        rows = []
        for r in self.results:
            rows.append((
                r.name,
                "Yes" if r.found else "No",
                r.version or "-",
                "Yes" if r.required else "No (optional)",
                r.status,
            ))
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        def _fmt_row(cells):
            return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

        lines = [_fmt_row(headers), _fmt_row(["-" * w for w in widths])]
        lines.extend(_fmt_row(row) for row in rows)
        return "\n".join(lines)

    def as_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "tools": [
                {
                    "name": r.name,
                    "found": r.found,
                    "version": r.version,
                    "required": r.required,
                    "status": r.status,
                    "path": r.path,
                    "notes": r.notes,
                }
                for r in self.results
            ],
        }


class DependencyValidationError(RuntimeError):
    """Raised when one or more required external tools are missing.

    Carries the full ``DependencyReport`` so callers (CLI, tests) can print
    a consolidated table alongside the error message.
    """

    def __init__(self, report: DependencyReport):
        self.report = report
        missing = ", ".join(r.name for r in report.missing_required)
        message = (
            f"Missing required external tool(s): {missing}.\n\n"
            f"{report.as_table()}\n\n"
            f"Install missing tools before running GEPER. See "
            f"docs/INSTALL_DEPENDENCIES.md or run ./install_dependencies.sh.\n"
            f"You can also run `python main.py verify-environment` for a full "
            f"environment check."
        )
        super().__init__(message)


# ─── Low-level discovery helpers (monkeypatchable in tests) ──────────────────

def _which(executable: str) -> Optional[str]:
    return shutil.which(executable)


def _run_version_command(path: str, version_args: tuple) -> Optional[str]:
    """Run `<path> <version_args>` and try to extract a version string.

    Returns None if the command fails or times out; never raises.
    """
    try:
        proc = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None

    output = (proc.stdout or "") + (proc.stderr or "")
    return _extract_version(output)


def _extract_version(output: str) -> Optional[str]:
    if not output:
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)", output)
    if match:
        return match.group(1)
    # Fall back to first non-empty line, truncated, if no numeric version found.
    first_line = next((ln.strip() for ln in output.splitlines() if ln.strip()), None)
    if first_line:
        return first_line[:60]
    return None


# ─── Core validation ──────────────────────────────────────────────────────────

def check_tool(spec: ToolSpec, which_fn: Optional[Callable] = None,
               version_fn: Optional[Callable] = None) -> ToolCheckResult:
    """Check a single tool against its candidate executable names.

    which_fn/version_fn default to the module-level _which/_run_version_command
    helpers, resolved dynamically at call time (not bound at import time),
    so tests can monkeypatch the module attributes directly.
    """
    which_fn = which_fn if which_fn is not None else _which
    version_fn = version_fn if version_fn is not None else _run_version_command
    found_path = None
    matched_name = spec.name
    for candidate in spec.candidates:
        p = which_fn(candidate)
        if p:
            found_path = p
            matched_name = candidate
            break

    if not found_path:
        return ToolCheckResult(
            name=spec.name,
            found=False,
            version=None,
            required=spec.required,
            status="MISSING" if spec.required else "WARNING",
            path=None,
            notes=spec.notes,
        )

    version = version_fn(found_path, spec.version_args)
    return ToolCheckResult(
        name=spec.name,
        found=True,
        version=version,
        required=spec.required,
        status="OK",
        path=found_path,
        notes=f"resolved as '{matched_name}'" if matched_name != spec.name else spec.notes,
    )


def validate_dependencies(
    require_minimap2: bool = False,
    require_vep: bool = False,
    which_fn: Optional[Callable] = None,
    version_fn: Optional[Callable] = None,
) -> DependencyReport:
    """Check all registered tools and return a consolidated report.

    Args:
        require_minimap2: if True, minimap2 is treated as required (e.g. the
            active config selects minimap2 as the aligner).
        require_vep: if True, VEP is treated as required instead of optional
            (e.g. the active config has vep.enabled=true and no fallback).
        which_fn / version_fn: injectable for unit testing.

    Returns:
        DependencyReport with one ToolCheckResult per registered tool.
    """
    report = DependencyReport()
    for spec in TOOL_SPECS:
        effective_spec = spec
        if spec.name == "minimap2" and require_minimap2:
            effective_spec = ToolSpec(
                name=spec.name, candidates=spec.candidates,
                version_args=spec.version_args, required=True, notes=spec.notes,
            )
        elif spec.name == "vep" and require_vep:
            effective_spec = ToolSpec(
                name=spec.name, candidates=spec.candidates,
                version_args=spec.version_args, required=True, notes=spec.notes,
            )
        report.results.append(check_tool(effective_spec, which_fn=which_fn, version_fn=version_fn))
    return report


def assert_required_dependencies(
    require_minimap2: bool = False,
    require_vep: bool = False,
) -> DependencyReport:
    """Validate dependencies and raise DependencyValidationError if any
    required tool is missing. Returns the report on success (all required
    tools found; optional-tool gaps only produce warnings, not exceptions).
    """
    report = validate_dependencies(require_minimap2=require_minimap2, require_vep=require_vep)
    if not report.ok:
        raise DependencyValidationError(report)
    return report
