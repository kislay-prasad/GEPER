"""
tests/test_verify_environment.py
───────────────────────────────────
Unit tests for verify_environment.py.

System probes (RAM, disk, CPU, CUDA) are monkeypatched so this test suite
is deterministic and does not depend on the host machine's actual hardware
or on real external tool binaries being installed.
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_environment as ve
from pipeline.utils.dependency_validator import DependencyReport, ToolCheckResult


# ─── check_python_version ──────────────────────────────────────────────────────


def test_check_python_version_pass_on_supported_version(monkeypatch):
    monkeypatch.setattr(ve.sys, "version_info", (3, 11, 0))
    result = ve.check_python_version()
    assert result.status == "PASS"


def test_check_python_version_fail_on_unsupported_version(monkeypatch):
    monkeypatch.setattr(ve.sys, "version_info", (3, 8, 0))
    result = ve.check_python_version()
    assert result.status == "FAIL"


# ─── check_ram ──────────────────────────────────────────────────────────────────


def test_check_ram_pass_when_plenty(monkeypatch):
    monkeypatch.setattr(ve, "_read_ram_gb", lambda: 32.0)
    result = ve.check_ram()
    assert result.status == "PASS"


def test_check_ram_warning_when_low_but_usable(monkeypatch):
    monkeypatch.setattr(ve, "_read_ram_gb", lambda: 4.0)
    result = ve.check_ram()
    assert result.status == "WARNING"


def test_check_ram_fail_when_critically_low(monkeypatch):
    monkeypatch.setattr(ve, "_read_ram_gb", lambda: 1.0)
    result = ve.check_ram()
    assert result.status == "FAIL"


def test_check_ram_warning_when_undetectable(monkeypatch):
    monkeypatch.setattr(ve, "_read_ram_gb", lambda: None)
    result = ve.check_ram()
    assert result.status == "WARNING"


# ─── check_cpu ──────────────────────────────────────────────────────────────────


def test_check_cpu_pass(monkeypatch):
    monkeypatch.setattr(ve.os, "cpu_count", lambda: 8)
    result = ve.check_cpu()
    assert result.status == "PASS"


def test_check_cpu_warning_low_cores(monkeypatch):
    monkeypatch.setattr(ve.os, "cpu_count", lambda: 1)
    result = ve.check_cpu()
    assert result.status == "WARNING"


# ─── check_disk_space ───────────────────────────────────────────────────────────


class _FakeUsage:
    def __init__(self, free_bytes):
        self.free = free_bytes
        self.total = free_bytes * 2
        self.used = free_bytes


def test_check_disk_space_pass(monkeypatch):
    monkeypatch.setattr(ve.shutil, "disk_usage", lambda path: _FakeUsage(50 * 1024**3))
    result = ve.check_disk_space(".")
    assert result.status == "PASS"


def test_check_disk_space_warning_low(monkeypatch):
    monkeypatch.setattr(ve.shutil, "disk_usage", lambda path: _FakeUsage(1 * 1024**3))
    result = ve.check_disk_space(".")
    assert result.status == "WARNING"


def test_check_disk_space_warning_on_error(monkeypatch):
    def _raise(path):
        raise OSError("boom")

    monkeypatch.setattr(ve.shutil, "disk_usage", _raise)
    result = ve.check_disk_space(".")
    assert result.status == "WARNING"


# ─── check_cuda_gpu ─────────────────────────────────────────────────────────────


def test_check_cuda_gpu_never_fails_when_absent(monkeypatch):
    """GPU is never required for the core pipeline — absence must be a
    WARNING, never a FAIL, regardless of torch/nvidia-smi availability."""
    monkeypatch.setattr(ve.shutil, "which", lambda name: None)
    result = ve.check_cuda_gpu()
    assert result.status in ("WARNING", "PASS")
    assert result.status != "FAIL"
    assert result.required is False


# ─── run_environment_checks / EnvironmentReport ───────────────────────────────


def _fake_tool_report(all_ok: bool) -> DependencyReport:
    report = DependencyReport()
    report.results.append(
        ToolCheckResult(
            name="bwa",
            found=all_ok,
            version="1.0" if all_ok else None,
            required=True,
            status="OK" if all_ok else "MISSING",
        )
    )
    return report


def test_overall_status_fail_when_required_tool_missing(monkeypatch):
    monkeypatch.setattr(
        ve, "check_python_version", lambda: ve.CheckResult("Python version", "PASS", "ok")
    )
    monkeypatch.setattr(ve, "check_ram", lambda: ve.CheckResult("RAM", "PASS", "ok"))
    monkeypatch.setattr(ve, "check_cpu", lambda: ve.CheckResult("CPU", "PASS", "ok"))
    monkeypatch.setattr(
        ve, "check_disk_space", lambda path=".": ve.CheckResult("Disk space", "PASS", "ok")
    )
    monkeypatch.setattr(
        ve,
        "check_cuda_gpu",
        lambda: ve.CheckResult("CUDA / GPU", "WARNING", "no gpu", required=False),
    )
    monkeypatch.setattr(ve, "check_external_tools", lambda: _fake_tool_report(all_ok=False))

    report = ve.run_environment_checks()
    assert report.overall_status == "FAIL"


def test_overall_status_pass_when_everything_ok(monkeypatch):
    monkeypatch.setattr(
        ve, "check_python_version", lambda: ve.CheckResult("Python version", "PASS", "ok")
    )
    monkeypatch.setattr(ve, "check_ram", lambda: ve.CheckResult("RAM", "PASS", "ok"))
    monkeypatch.setattr(ve, "check_cpu", lambda: ve.CheckResult("CPU", "PASS", "ok"))
    monkeypatch.setattr(
        ve, "check_disk_space", lambda path=".": ve.CheckResult("Disk space", "PASS", "ok")
    )
    monkeypatch.setattr(
        ve,
        "check_cuda_gpu",
        lambda: ve.CheckResult("CUDA / GPU", "PASS", "gpu found", required=False),
    )
    monkeypatch.setattr(ve, "check_external_tools", lambda: _fake_tool_report(all_ok=True))

    report = ve.run_environment_checks()
    assert report.overall_status == "PASS"


def test_render_includes_banner_and_overall_status(monkeypatch):
    monkeypatch.setattr(
        ve, "check_python_version", lambda: ve.CheckResult("Python version", "PASS", "ok")
    )
    monkeypatch.setattr(ve, "check_ram", lambda: ve.CheckResult("RAM", "PASS", "ok"))
    monkeypatch.setattr(ve, "check_cpu", lambda: ve.CheckResult("CPU", "PASS", "ok"))
    monkeypatch.setattr(
        ve, "check_disk_space", lambda path=".": ve.CheckResult("Disk space", "PASS", "ok")
    )
    monkeypatch.setattr(
        ve,
        "check_cuda_gpu",
        lambda: ve.CheckResult("CUDA / GPU", "PASS", "gpu found", required=False),
    )
    monkeypatch.setattr(ve, "check_external_tools", lambda: _fake_tool_report(all_ok=True))

    report = ve.run_environment_checks()
    text = report.render()
    assert "Bij AI Environment Verification" in text
    assert "Overall: PASS" in text


def test_as_dict_structure(monkeypatch):
    monkeypatch.setattr(
        ve, "check_python_version", lambda: ve.CheckResult("Python version", "PASS", "ok")
    )
    monkeypatch.setattr(ve, "check_ram", lambda: ve.CheckResult("RAM", "PASS", "ok"))
    monkeypatch.setattr(ve, "check_cpu", lambda: ve.CheckResult("CPU", "PASS", "ok"))
    monkeypatch.setattr(
        ve, "check_disk_space", lambda path=".": ve.CheckResult("Disk space", "PASS", "ok")
    )
    monkeypatch.setattr(
        ve,
        "check_cuda_gpu",
        lambda: ve.CheckResult("CUDA / GPU", "PASS", "gpu found", required=False),
    )
    monkeypatch.setattr(ve, "check_external_tools", lambda: _fake_tool_report(all_ok=True))

    report = ve.run_environment_checks()
    d = report.as_dict()
    assert d["overall_status"] == "PASS"
    assert "checks" in d
    assert "tools" in d


def test_main_returns_zero_on_pass(monkeypatch, capsys):
    monkeypatch.setattr(ve, "run_environment_checks", lambda disk_path=".": _passing_report())
    rc = ve.main()
    assert rc == 0


def test_main_returns_one_on_fail(monkeypatch, capsys):
    monkeypatch.setattr(ve, "run_environment_checks", lambda disk_path=".": _failing_report())
    rc = ve.main()
    assert rc == 1


def _passing_report():
    r = ve.EnvironmentReport()
    r.checks.append(ve.CheckResult("Python version", "PASS", "ok"))
    r.tool_report = _fake_tool_report(all_ok=True)
    return r


def _failing_report():
    r = ve.EnvironmentReport()
    r.checks.append(ve.CheckResult("Python version", "FAIL", "too old"))
    r.tool_report = _fake_tool_report(all_ok=True)
    return r
