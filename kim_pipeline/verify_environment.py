#!/usr/bin/env python3
"""
verify_environment.py
──────────────────────
Standalone GEPER environment verification.

Checks, independently of any pipeline stage:
  - Python version
  - RAM
  - CPU (core count)
  - CUDA / GPU availability
  - Disk space
  - Every external bioinformatics tool GEPER depends on
    (bwa, minimap2, samtools, freebayes, bcftools, vep)

Each check reports PASS / WARNING / FAIL. The overall result is:
  - FAIL     if any required check fails
  - WARNING  if no required check fails but at least one optional/soft
             check (e.g. low disk space, no GPU, missing optional tool)
             produced a warning
  - PASS     otherwise

This module does not import PipelineRunner or any pipeline stage — it is
a pure, read-only environment probe. It can be run directly:

    python verify_environment.py

or invoked via the CLI:

    python main.py verify-environment

or imported:

    from verify_environment import run_environment_checks
    report = run_environment_checks()
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from pipeline.utils.dependency_validator import (
    TOOL_SPECS,
    check_tool,
    DependencyReport,
)

MIN_PYTHON = (3, 10)
MIN_RAM_GB_WARN = 8.0  # below this -> WARNING (pipeline may still run)
MIN_RAM_GB_FAIL = 2.0  # below this -> FAIL (pipeline cannot run at all)
MIN_DISK_GB_WARN = 10.0
MIN_CPU_CORES_WARN = 2


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "WARNING" | "FAIL"
    detail: str
    required: bool = True


@dataclass
class EnvironmentReport:
    checks: List[CheckResult] = field(default_factory=list)
    tool_report: Optional[DependencyReport] = None

    @property
    def overall_status(self) -> str:
        statuses = [c.status for c in self.checks]
        if self.tool_report is not None:
            statuses.extend(
                "FAIL" if (r.required and not r.found) else ("WARNING" if not r.found else "PASS")
                for r in self.tool_report.results
            )
        if "FAIL" in statuses:
            return "FAIL"
        if "WARNING" in statuses:
            return "WARNING"
        return "PASS"

    def render(self) -> str:
        from pipeline.reporting.component_identity import COMPONENT_NAME

        lines = ["=" * 60, f"{COMPONENT_NAME} Environment Verification", "=" * 60]
        for c in self.checks:
            lines.append(f"[{c.status:7s}] {c.name}: {c.detail}")
        if self.tool_report is not None:
            lines.append("")
            lines.append("External tools:")
            lines.append(self.tool_report.as_table())
        lines.append("")
        lines.append(f"Overall: {self.overall_status}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def as_dict(self) -> dict:
        d = {
            "overall_status": self.overall_status,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "required": c.required}
                for c in self.checks
            ],
        }
        if self.tool_report is not None:
            d["tools"] = self.tool_report.as_dict()
        return d


# ─── Individual checks ────────────────────────────────────────────────────────


def check_python_version() -> CheckResult:
    current = sys.version_info[:2]
    detail = f"{platform.python_version()} (required >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"
    if current >= MIN_PYTHON:
        return CheckResult("Python version", "PASS", detail)
    return CheckResult("Python version", "FAIL", detail)


def _read_ram_gb() -> Optional[float]:
    """Best-effort RAM detection without requiring psutil."""
    try:
        import psutil  # type: ignore

        return psutil.virtual_memory().total / (1024**3)
    except Exception:
        pass
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
    except Exception:
        pass
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            if out.returncode == 0 and out.stdout.strip():
                return int(out.stdout.strip()) / (1024**3)
    except Exception:
        pass
    return None


def check_ram() -> CheckResult:
    ram_gb = _read_ram_gb()
    if ram_gb is None:
        return CheckResult("RAM", "WARNING", "Could not determine total RAM.")
    detail = f"{ram_gb:.1f} GB detected"
    if ram_gb < MIN_RAM_GB_FAIL:
        return CheckResult("RAM", "FAIL", detail + f" (minimum {MIN_RAM_GB_FAIL} GB required)")
    if ram_gb < MIN_RAM_GB_WARN:
        return CheckResult(
            "RAM",
            "WARNING",
            detail + f" ({MIN_RAM_GB_WARN} GB recommended; AI model stages may be constrained)",
        )
    return CheckResult("RAM", "PASS", detail)


def check_cpu() -> CheckResult:
    cores = os.cpu_count() or 0
    detail = f"{cores} logical core(s) detected"
    if cores == 0:
        return CheckResult("CPU", "WARNING", "Could not determine CPU core count.")
    if cores < MIN_CPU_CORES_WARN:
        return CheckResult("CPU", "WARNING", detail + f" ({MIN_CPU_CORES_WARN}+ recommended)")
    return CheckResult("CPU", "PASS", detail)


def check_disk_space(path: str = ".") -> CheckResult:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024**3)
    except Exception as exc:
        return CheckResult("Disk space", "WARNING", f"Could not determine disk usage: {exc}")
    detail = f"{free_gb:.1f} GB free at '{os.path.abspath(path)}'"
    if free_gb < MIN_DISK_GB_WARN:
        return CheckResult(
            "Disk space",
            "WARNING",
            detail + f" ({MIN_DISK_GB_WARN} GB recommended for alignment/VCF intermediates)",
        )
    return CheckResult("Disk space", "PASS", detail)


def check_cuda_gpu() -> CheckResult:
    """GPU/CUDA is never required for the core pipeline (only for optional
    AI-model stages, which are explicitly out of scope for this task) —
    this check is always a WARNING (not FAIL) when unavailable."""
    # Prefer torch if it happens to be installed, since it gives the most
    # accurate picture of what the AI stages would see. Never import torch
    # as a hard dependency.
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            return CheckResult("CUDA / GPU", "PASS", f"CUDA available — {name}", required=False)
        return CheckResult(
            "CUDA / GPU",
            "WARNING",
            "torch installed but no CUDA device available (CPU-only mode).",
            required=False,
        )
    except ImportError:
        pass
    except Exception as exc:
        return CheckResult(
            "CUDA / GPU", "WARNING", f"torch present but CUDA probe failed: {exc}", required=False
        )

    # Fall back to nvidia-smi presence as a coarse signal.
    if shutil.which("nvidia-smi"):
        return CheckResult(
            "CUDA / GPU",
            "WARNING",
            "nvidia-smi found but torch not installed — GPU cannot be confirmed usable.",
            required=False,
        )
    return CheckResult(
        "CUDA / GPU",
        "WARNING",
        "No GPU/CUDA detected (not required for core pipeline; needed only for optional AI stages).",
        required=False,
    )


def check_external_tools() -> DependencyReport:
    report = DependencyReport()
    for spec in TOOL_SPECS:
        report.results.append(check_tool(spec))
    return report


def run_environment_checks(disk_path: str = ".") -> EnvironmentReport:
    report = EnvironmentReport()
    report.checks.append(check_python_version())
    report.checks.append(check_ram())
    report.checks.append(check_cpu())
    report.checks.append(check_disk_space(disk_path))
    report.checks.append(check_cuda_gpu())
    report.tool_report = check_external_tools()
    return report


def main() -> int:
    report = run_environment_checks()
    print(report.render())
    status = report.overall_status
    if status == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
