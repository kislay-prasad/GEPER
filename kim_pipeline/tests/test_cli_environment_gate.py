"""
tests/test_cli_environment_gate.py
─────────────────────────────────────
Tests for the CLI-layer startup validation gate added to `main.py`:
  - `_print_startup_validation` (the pre-flight dependency/file check
    invoked by `cmd_analyze` before PipelineRunner is ever constructed)
  - `cmd_verify_environment` (the new `verify-environment` subcommand)

These tests never touch PipelineRunner or any pipeline stage; they only
exercise the CLI pre-flight layer, with tool discovery and filesystem
checks mocked out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as geper_main
from pipeline.utils.dependency_validator import DependencyValidationError


def _make_args(**overrides):
    ns = argparse.Namespace(
        r1="sample_R1.fastq.gz",
        r2=None,
        ref="reference.fasta",
        output_dir="/tmp/out",
        sample_id=None,
        config=None,
        log_level="INFO",
        no_resume=False,
        cadd=None,
        revel=None,
        spliceai=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ─── _print_startup_validation ─────────────────────────────────────────────────


def test_startup_validation_raises_dependency_error_when_tools_missing(monkeypatch, capsys):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", lambda name: None)

    args = _make_args()
    with pytest.raises(DependencyValidationError):
        geper_main._print_startup_validation(args, cfg={})

    out = capsys.readouterr().out
    assert "Bij AI Environment Validation" in out


def test_startup_validation_passes_when_tools_present_and_files_exist(monkeypatch, tmp_path):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dv, "_run_version_command", lambda path, args: "1.0.0")

    ref = tmp_path / "ref.fasta"
    ref.write_text(">chr1\nACGT\n")
    r1 = tmp_path / "sample_R1.fastq.gz"
    r1.write_bytes(b"\x1f\x8b\x00")

    args = _make_args(ref=str(ref), r1=str(r1))
    # Should not raise.
    geper_main._print_startup_validation(args, cfg={})


def test_startup_validation_raises_file_not_found_for_missing_reference(monkeypatch, tmp_path):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dv, "_run_version_command", lambda path, args: "1.0.0")

    r1 = tmp_path / "sample_R1.fastq.gz"
    r1.write_bytes(b"\x1f\x8b\x00")

    args = _make_args(ref="does_not_exist.fasta", r1=str(r1))
    with pytest.raises(FileNotFoundError):
        geper_main._print_startup_validation(args, cfg={})


def test_startup_validation_respects_minimap2_requirement(monkeypatch, tmp_path):
    """If config selects minimap2 as the aligner, bwa alone being present
    should not be enough — minimap2 becomes required too."""
    import pipeline.utils.dependency_validator as dv

    def which_fn(name):
        if name in ("bwa-mem2", "bwa", "samtools", "freebayes", "bcftools"):
            return f"/usr/bin/{name}"
        return None  # minimap2, vep absent

    monkeypatch.setattr(dv, "_which", which_fn)
    monkeypatch.setattr(dv, "_run_version_command", lambda path, args: "1.0.0")

    ref = tmp_path / "ref.fasta"
    ref.write_text(">chr1\nACGT\n")
    r1 = tmp_path / "sample_R1.fastq.gz"
    r1.write_bytes(b"\x1f\x8b\x00")

    args = _make_args(ref=str(ref), r1=str(r1))
    cfg = {"alignment": {"aligner": "minimap2"}}

    with pytest.raises(DependencyValidationError) as exc_info:
        geper_main._print_startup_validation(args, cfg=cfg)
    assert "minimap2" in str(exc_info.value)


# ─── cmd_analyze return codes on gate failure ─────────────────────────────────


def test_cmd_analyze_returns_3_on_missing_required_tools(monkeypatch, tmp_path):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", lambda name: None)
    monkeypatch.setattr(geper_main, "_load_config", lambda path: {})

    args = _make_args(output_dir=str(tmp_path))
    rc = geper_main.cmd_analyze(args)
    assert rc == 3


def test_cmd_analyze_returns_1_on_missing_input_file(monkeypatch, tmp_path):
    import pipeline.utils.dependency_validator as dv

    monkeypatch.setattr(dv, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dv, "_run_version_command", lambda path, args: "1.0.0")
    monkeypatch.setattr(geper_main, "_load_config", lambda path: {})

    args = _make_args(output_dir=str(tmp_path), ref="nope.fasta", r1="also_nope.fastq.gz")
    rc = geper_main.cmd_analyze(args)
    assert rc == 1


# ─── cmd_verify_environment ─────────────────────────────────────────────────────


def test_cmd_verify_environment_returns_zero_on_pass(monkeypatch, capsys):
    import verify_environment as ve

    def fake_run_checks():
        report = ve.EnvironmentReport()
        report.checks.append(ve.CheckResult("Python version", "PASS", "ok"))
        from pipeline.utils.dependency_validator import DependencyReport

        report.tool_report = DependencyReport()
        return report

    monkeypatch.setattr(ve, "run_environment_checks", fake_run_checks)

    args = argparse.Namespace(log_level="INFO", json=False)
    rc = geper_main.cmd_verify_environment(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "GEPER Environment Verification" in out


def test_cmd_verify_environment_returns_one_on_fail(monkeypatch, capsys):
    import verify_environment as ve

    def fake_run_checks():
        report = ve.EnvironmentReport()
        report.checks.append(ve.CheckResult("Python version", "FAIL", "too old"))
        from pipeline.utils.dependency_validator import DependencyReport

        report.tool_report = DependencyReport()
        return report

    monkeypatch.setattr(ve, "run_environment_checks", fake_run_checks)

    args = argparse.Namespace(log_level="INFO", json=False)
    rc = geper_main.cmd_verify_environment(args)
    assert rc == 1


def test_cmd_verify_environment_json_output(monkeypatch, capsys):
    import verify_environment as ve

    def fake_run_checks():
        report = ve.EnvironmentReport()
        report.checks.append(ve.CheckResult("Python version", "PASS", "ok"))
        from pipeline.utils.dependency_validator import DependencyReport

        report.tool_report = DependencyReport()
        return report

    monkeypatch.setattr(ve, "run_environment_checks", fake_run_checks)

    args = argparse.Namespace(log_level="INFO", json=True)
    rc = geper_main.cmd_verify_environment(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["overall_status"] == "PASS"


# ─── CLI parser wiring (backward compatibility check) ─────────────────────────


def test_all_original_subcommands_still_registered():
    parser = geper_main._build_parser()
    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert sub_actions, "no subparsers action found"
    choices = sub_actions[0].choices
    for cmd in ("analyze", "vcf", "classify", "serve", "validate", "test"):
        assert cmd in choices, (
            f"existing subcommand '{cmd}' missing — backward compatibility broken"
        )


def test_verify_environment_subcommand_registered():
    parser = geper_main._build_parser()
    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    choices = sub_actions[0].choices
    assert "verify-environment" in choices
