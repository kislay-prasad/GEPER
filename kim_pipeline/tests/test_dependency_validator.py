"""
tests/test_dependency_validator.py
────────────────────────────────────
Unit tests for pipeline.utils.dependency_validator.

All external tool discovery is mocked — no real `bwa`/`samtools`/etc.
binaries are required to run this test suite.
"""

from __future__ import annotations

import pytest

from pipeline.utils.dependency_validator import (
    TOOL_SPECS,
    ToolSpec,
    check_tool,
    validate_dependencies,
    DependencyValidationError,
    DependencyReport,
)


def _fake_which_all_present(name: str):
    return f"/usr/bin/{name}"


def _fake_which_none_present(name: str):
    return None


def _fake_version(path: str, args):
    return "1.2.3"


# ─── check_tool ────────────────────────────────────────────────────────────────


def test_check_tool_found_required():
    spec = ToolSpec(name="samtools", candidates=("samtools",), required=True)
    result = check_tool(spec, which_fn=_fake_which_all_present, version_fn=_fake_version)
    assert result.found is True
    assert result.status == "OK"
    assert result.version == "1.2.3"
    assert result.path == "/usr/bin/samtools"


def test_check_tool_missing_required():
    spec = ToolSpec(name="samtools", candidates=("samtools",), required=True)
    result = check_tool(spec, which_fn=_fake_which_none_present, version_fn=_fake_version)
    assert result.found is False
    assert result.status == "MISSING"


def test_check_tool_missing_optional_is_warning_not_missing():
    spec = ToolSpec(name="vep", candidates=("vep",), required=False)
    result = check_tool(spec, which_fn=_fake_which_none_present, version_fn=_fake_version)
    assert result.found is False
    assert result.status == "WARNING"
    assert result.required is False


def test_check_tool_candidate_fallback():
    """bwa-mem2 missing, bwa present -> should resolve via second candidate."""

    def which_fn(name):
        return "/usr/bin/bwa" if name == "bwa" else None

    spec = ToolSpec(name="bwa", candidates=("bwa-mem2", "bwa"), required=True)
    result = check_tool(spec, which_fn=which_fn, version_fn=_fake_version)
    assert result.found is True
    assert result.path == "/usr/bin/bwa"


# ─── validate_dependencies / DependencyReport ─────────────────────────────────


def test_validate_dependencies_all_present():
    report = validate_dependencies(which_fn=_fake_which_all_present, version_fn=_fake_version)
    assert report.ok is True
    assert report.missing_required == []
    assert len(report.results) == len(TOOL_SPECS)


def test_validate_dependencies_missing_required_tools():
    report = validate_dependencies(which_fn=_fake_which_none_present, version_fn=_fake_version)
    assert report.ok is False
    missing_names = {r.name for r in report.missing_required}
    assert missing_names == {"bwa", "samtools", "freebayes", "bcftools"}


def test_validate_dependencies_require_minimap2_flag():
    report = validate_dependencies(
        require_minimap2=True,
        which_fn=_fake_which_none_present,
        version_fn=_fake_version,
    )
    missing_names = {r.name for r in report.missing_required}
    assert "minimap2" in missing_names


def test_validate_dependencies_require_vep_flag():
    report = validate_dependencies(
        require_vep=True,
        which_fn=_fake_which_none_present,
        version_fn=_fake_version,
    )
    missing_names = {r.name for r in report.missing_required}
    assert "vep" in missing_names


def test_report_as_table_contains_headers_and_rows():
    report = validate_dependencies(which_fn=_fake_which_all_present, version_fn=_fake_version)
    table = report.as_table()
    assert "Tool" in table
    assert "Found?" in table
    assert "Version" in table
    assert "Required?" in table
    assert "Status" in table
    for spec in TOOL_SPECS:
        assert spec.name in table


def test_report_as_dict_roundtrip():
    report = validate_dependencies(which_fn=_fake_which_all_present, version_fn=_fake_version)
    d = report.as_dict()
    assert d["ok"] is True
    assert len(d["tools"]) == len(TOOL_SPECS)
    assert all("status" in t for t in d["tools"])


# ─── assert_required_dependencies / DependencyValidationError ─────────────────


def test_assert_required_dependencies_returns_report_type():
    report = assert_required_dependencies_with_mocks(_fake_which_all_present, _fake_version)
    assert isinstance(report, DependencyReport)
    assert report.ok is True


def assert_required_dependencies_with_mocks(which_fn, version_fn):
    report = validate_dependencies(which_fn=which_fn, version_fn=version_fn)
    if not report.ok:
        raise DependencyValidationError(report)
    return report


def test_assert_required_dependencies_raises_with_consolidated_message(monkeypatch):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", _fake_which_none_present)
    monkeypatch.setattr(dv, "_run_version_command", _fake_version)

    with pytest.raises(DependencyValidationError) as exc_info:
        dv.assert_required_dependencies()

    err = exc_info.value
    assert "bwa" in str(err)
    assert "samtools" in str(err)
    assert "freebayes" in str(err)
    assert "bcftools" in str(err)
    assert isinstance(err.report, DependencyReport)
    assert len(err.report.missing_required) == 4


def test_dependency_validation_error_message_mentions_install_docs(monkeypatch):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", _fake_which_none_present)
    monkeypatch.setattr(dv, "_run_version_command", _fake_version)

    with pytest.raises(DependencyValidationError) as exc_info:
        dv.assert_required_dependencies()

    message = str(exc_info.value)
    assert "INSTALL_DEPENDENCIES.md" in message
    assert "install_dependencies.sh" in message
    assert "verify-environment" in message


def test_optional_tools_never_raise(monkeypatch):
    """If only optional tools (minimap2, vep) are missing, no exception."""
    import pipeline.utils.dependency_validator as dv

    def which_fn(name):
        if name in ("bwa-mem2", "bwa", "samtools", "freebayes", "bcftools"):
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(dv, "_which", which_fn)
    monkeypatch.setattr(dv, "_run_version_command", _fake_version)

    report = dv.assert_required_dependencies()
    assert report.ok is True
    warnings = [r for r in report.results if r.status == "WARNING"]
    assert {"minimap2", "vep"} <= {r.name for r in warnings}
