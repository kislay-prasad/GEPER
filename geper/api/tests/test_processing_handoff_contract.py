"""
api/tests/test_processing_handoff_contract.py
─────────────────────────────────────────────
No database. The command line the interpretation worker builds must be one `geper/main.py` accepts, and the stand-in
engine used by `test_processing_handoff.py` must behave as the real CLI's contract says.

Until now the worker's own tests patched `spawn_tracked` and never parsed the command it built, so a flag renamed in
`geper/main.py` would have left every worker test green. Here the worker's command is parsed by

  * a parser built from the syntax tree of the real `build_arg_parser()` (always; the same parser the stand-in uses), and
  * the real `build_arg_parser()` itself, when this interpreter is one `geper/main.py` will start on (3.11 to 3.12).

SYNTHETIC FIXTURES ONLY. Nothing here says anything about the genomic pipeline's accuracy or clinical validity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api import submission_worker
from api.submission_store import SubmissionStore
from api.submission_worker import InterpretationWorker
from api.tests import standin_engine

STANDIN = Path(standin_engine.__file__)
VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
    "chr2\t2000\t.\tG\tC\t40\tPASS\t.\tGT\t1/1\n"
)
HPO = {"terms": ["HP:0001250", "HP:0002011"]}
QC = {"mean_coverage_depth": 45.2}


@pytest.fixture
def vcf(tmp_path):
    path = tmp_path / "input.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return path


def _worker_command(tmp_path, vcf, *, hpo_terms, qc_metrics, monkeypatch):
    """The exact command the worker hands to `spawn_tracked` for one submission, captured without running anything."""
    monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", str(tmp_path / "out"))
    store = SubmissionStore(tmp_path / "s.db")
    submission = store.create_submission(
        org_id="00000000-0000-4000-8000-000000000001",
        submission_key="contract-1",
        vcf_path=str(vcf),
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
        order_id="00000000-0000-4000-8000-000000000002",
        sample_id="00000000-0000-4000-8000-000000000003",
        hpo_terms=hpo_terms,
        qc_metrics=qc_metrics,
    )
    worker = InterpretationWorker(store, data_access=MagicMock())
    worker._clinical_preflight = lambda sub: SimpleNamespace(vcf_hash="0" * 64)
    worker._adopt_existing_record = lambda *a, **k: False
    worker._record_run = lambda *a, **k: True
    captured = {}

    def fake_spawn(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "geper_results.json").write_text("{}", encoding="utf-8")
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    with patch.object(submission_worker, "spawn_tracked", fake_spawn):
        assert worker.process_queued_submission(submission) is True
    return captured["cmd"]


COMMANDS = {
    "minimal": dict(hpo_terms=None, qc_metrics=None),
    "with_hpo_and_qc": dict(hpo_terms=HPO, qc_metrics=QC),
}


@pytest.mark.parametrize("case", sorted(COMMANDS))
def test_the_worker_command_is_one_the_real_cli_declares(tmp_path, vcf, monkeypatch, case):
    cmd = _worker_command(tmp_path, vcf, monkeypatch=monkeypatch, **COMMANDS[case])
    assert cmd[0] == sys.executable and cmd[1].endswith("main.py")
    flags_passed = [token for token in cmd[2:] if token.startswith("--")]
    declared = standin_engine.declared_arguments()
    declared_flags = {flag for spec in declared for flag in spec["flags"]}
    assert set(flags_passed) <= declared_flags, sorted(set(flags_passed) - declared_flags)
    for spec in declared:
        if spec["required"]:
            assert any(flag in flags_passed for flag in spec["flags"]), f"required {spec['flags']} not passed"
    # and the whole command parses under the parser built from the real CLI's declarations
    args = standin_engine.build_parser().parse_args(cmd[2:])
    assert args.vcf == str(vcf) and args.assembly == "GRCh38"
    assert args.no_resume is True  # an earlier attempt's checkpoint is never resumed
    if COMMANDS[case]["hpo_terms"]:
        assert args.hpo_terms == "HP:0001250,HP:0002011"
    if COMMANDS[case]["qc_metrics"]:
        assert args.qc_metrics_json.endswith("qc_metrics.json")


@pytest.mark.skipif(
    not ((3, 11) <= sys.version_info[:2] < (3, 13)),
    reason="geper/main.py refuses to start outside Python 3.11 to 3.12; the syntax-tree parser above still ran",
)
@pytest.mark.parametrize("case", sorted(COMMANDS))
def test_the_worker_command_parses_under_the_real_cli_parser(tmp_path, vcf, monkeypatch, case):
    from main import build_arg_parser  # geper/main.py itself

    cmd = _worker_command(tmp_path, vcf, monkeypatch=monkeypatch, **COMMANDS[case])
    parsed = build_arg_parser().parse_args(cmd[2:])  # exits the test on any argument the real CLI would refuse
    assert parsed.vcf == str(vcf) and parsed.assembly == "GRCh38"


def test_the_syntax_tree_reading_finds_the_arguments_the_worker_depends_on():
    """A guard on the guard: if the reading of `geper/main.py` ever returned nothing, the tests above would be vacuous."""
    by_flag = {flag: spec for spec in standin_engine.declared_arguments() for flag in spec["flags"]}
    for flag in ("--vcf", "--output-dir", "--assembly", "--hpo-terms", "--qc-metrics-json", "--no-resume"):
        assert flag in by_flag, flag
    assert by_flag["--vcf"]["required"] is True
    assert by_flag["--no-resume"]["action"] == "store_true"


# ── the stand-in itself ──


def _run_standin(tmp_path, *extra, mode="success", env_extra=None):
    import os

    env = dict(os.environ, STANDIN_MODE=mode, STANDIN_LOG=str(tmp_path / "standin.log"))
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(STANDIN), *extra], capture_output=True, text=True, env=env, timeout=60, check=False
    )


def test_the_standin_rejects_an_argument_the_real_cli_does_not_have(tmp_path, vcf):
    result = _run_standin(tmp_path, "--vcf", str(vcf), "--output-dir", str(tmp_path), "--not-a-real-flag", "x")
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr


def test_the_standin_requires_what_the_real_cli_requires(tmp_path):
    result = _run_standin(tmp_path, "--output-dir", str(tmp_path))
    assert result.returncode == 2 and "--vcf" in result.stderr


@pytest.mark.parametrize(
    "mode, exit_code, writes",
    [
        ("success", 0, "document"),
        ("exit1", 1, None),
        ("exit130", 130, None),
        ("no_output", 0, None),
        ("malformed_json", 0, "invalid"),
        ("empty_object", 0, "{}"),
    ],
)
def test_the_standin_modes_follow_the_cli_output_contract(tmp_path, vcf, mode, exit_code, writes):
    out = tmp_path / "out"
    result = _run_standin(tmp_path, "--vcf", str(vcf), "--assembly", "GRCh38", "--output-dir", str(out), mode=mode)
    assert result.returncode == exit_code
    results = out / "geper_results.json"
    if writes is None:
        assert not results.exists()
        return
    text = results.read_text(encoding="utf-8")
    if writes == "document":
        document = json.loads(text)
        assert document["variant_count"] == 2 and document["assembly"] == "GRCh38"
        assert [v["variant"]["pos"] for v in document["variants"]] == [1000, 2000]
        assert document["review_status"] == "draft"
        assert document["standin"]["argv"][:2] == ["--vcf", str(vcf)]
    elif writes == "{}":
        assert json.loads(text) == {}
    else:
        with pytest.raises(ValueError):
            json.loads(text)


def test_the_standin_logs_every_invocation_before_working(tmp_path, vcf):
    for _ in range(2):
        _run_standin(tmp_path, "--vcf", str(vcf), "--output-dir", str(tmp_path / "o"), mode="exit1")
    lines = (tmp_path / "standin.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["mode"] == "exit1"


def test_fail_once_fails_the_first_invocation_and_succeeds_afterwards(tmp_path, vcf):
    args = ("--vcf", str(vcf), "--assembly", "GRCh38", "--output-dir", str(tmp_path / "o"))
    assert _run_standin(tmp_path, *args, mode="fail_once").returncode == 1
    assert _run_standin(tmp_path, *args, mode="fail_once").returncode == 0
    assert (tmp_path / "o" / "geper_results.json").exists()


# ── F2: what counts as a finished engine output (no database) ──


@pytest.fixture
def good_document(tmp_path, vcf):
    out = tmp_path / "good-out"
    result = _run_standin(tmp_path, "--vcf", str(vcf), "--assembly", "GRCh38", "--output-dir", str(out))
    assert result.returncode == 0
    return json.loads((out / "geper_results.json").read_text(encoding="utf-8"))


def test_the_document_the_standin_writes_satisfies_the_worker_contract(good_document, vcf):
    assert submission_worker.validate_engine_output(good_document, str(vcf)) == []


def _mutate(**changes):
    def apply(document):
        document = dict(document)
        for key, value in changes.items():
            if value is _MISSING:
                document.pop(key, None)
            else:
                document[key] = value
        return document

    return apply


_MISSING = object()

INVALID_DOCUMENTS = {
    "empty_object": (
        lambda d: {},
        {"run_incomplete", "variants_not_a_list", "engine_output_claims_review", "input_vcf_missing"},
    ),
    "not_an_object_list": (lambda d: [], {"output_not_an_object"}),
    "not_an_object_null": (lambda d: None, {"output_not_an_object"}),
    "not_an_object_string": (lambda d: "done", {"output_not_an_object"}),
    "checkpoint_run_complete_false": (_mutate(run_complete=False), {"run_incomplete"}),
    "run_complete_missing": (_mutate(run_complete=_MISSING), {"run_incomplete"}),
    "run_complete_truthy_not_true": (_mutate(run_complete="true"), {"run_incomplete"}),
    "variants_missing": (_mutate(variants=_MISSING, variant_count=0), {"variants_not_a_list"}),
    "variants_a_dict": (_mutate(variants={"a": 1}), {"variants_not_a_list"}),
    "variant_entry_not_an_object": (_mutate(variants=["x", "y"], variant_count=2), {"variant_entry_malformed"}),
    "variant_entry_without_variant": (
        _mutate(variants=[{"nope": 1}, {"nope": 2}], variant_count=2),
        {"variant_entry_malformed"},
    ),
    "count_too_high": (_mutate(variant_count=99), {"variant_count_inconsistent"}),
    "count_missing": (_mutate(variant_count=_MISSING), {"variant_count_inconsistent"}),
    "count_a_string": (_mutate(variant_count="2"), {"variant_count_inconsistent"}),
    "count_a_bool": (_mutate(variant_count=True), {"variant_count_inconsistent"}),
    "claims_reviewed": (_mutate(review_status="reviewed"), {"engine_output_claims_review"}),
    "carries_a_reviewer": (_mutate(reviewed_by="Dr. A"), {"engine_output_claims_review"}),
    "carries_a_review_time": (_mutate(reviewed_at="2026-01-01T00:00:00+00:00"), {"engine_output_claims_review"}),
    "vcf_path_missing": (_mutate(input_vcf_absolute_path=_MISSING), {"input_vcf_missing"}),
    "vcf_path_empty": (_mutate(input_vcf_absolute_path=""), {"input_vcf_missing"}),
    "vcf_path_not_a_string": (_mutate(input_vcf_absolute_path=7), {"input_vcf_missing"}),
    "output_for_a_different_vcf": (
        _mutate(input_vcf_absolute_path="/somewhere/else.vcf"),
        {"output_is_for_a_different_vcf"},
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID_DOCUMENTS))
def test_invalid_engine_output_is_named_and_rejected(good_document, vcf, case):
    mutate, expected = INVALID_DOCUMENTS[case]
    problems = submission_worker.validate_engine_output(mutate(good_document), str(vcf))
    assert expected <= set(problems), problems
    assert problems  # never an empty list for an invalid document


def test_a_relative_or_differently_spelled_path_to_the_same_vcf_is_accepted(good_document, vcf, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    relative = os.path.relpath(str(vcf), tmp_path)
    document = dict(good_document, input_vcf_absolute_path=str(vcf))
    assert submission_worker.validate_engine_output(document, relative) == []
