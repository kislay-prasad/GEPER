"""
bridge/tests/test_bridge.py
────────────────────────────
Validates the Kim <-> GEPER bridge without requiring either project's
real (heavy) dependencies. Each project root is replaced with a tiny
fake `main.py` that mimics its real CLI contract:

  * fake Kim main.py:   writes <output_dir>/<sample_id>/checkpoint.json
                        with a `variant_calling.filtered_vcf_path` key,
                        exactly like the real `analyze --mode vcf_only`.
  * fake GEPER main.py: reads --vcf and writes geper_results.json /
                        geper_report.md to --output-dir, exactly like
                        the real `main.py --vcf ...`.

This proves the bridge's wiring (argument passing, checkpoint reading,
output-file discovery, error propagation) is correct, independent of
whether bwa/freebayes/torch/etc. are installed in this environment.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bridge.combined_pipeline import (
    BridgeError,
    qc_metrics_from_kim_checkpoint,
    run_combined,
    run_geper_vcf_to_report,
    run_kim_fastq_to_vcf,
    write_qc_metrics_sidecar,
)


FAKE_KIM_MAIN = textwrap.dedent(
    """
    import argparse, json, sys
    from pathlib import Path

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("analyze")
    a.add_argument("--r1", required=True)
    a.add_argument("--r2", default=None)
    a.add_argument("--ref", required=True)
    a.add_argument("--output-dir", required=True)
    a.add_argument("--sample-id", required=True)
    a.add_argument("--mode", default="full")
    a.add_argument("--config", default=None)
    a.add_argument("--no-resume", action="store_true")
    args = p.parse_args()

    work_dir = Path(args.output_dir) / args.sample_id
    work_dir.mkdir(parents=True, exist_ok=True)
    vcf_path = work_dir / "filtered_variants.vcf"
    vcf_path.write_text("##fileformat=VCFv4.2\\n#CHROM\\tPOS\\tID\\tREF\\tALT\\n")

    checkpoint = {
        "completed_stages": ["fastq_validation", "qc", "alignment", "variant_calling"],
        "qc_r1": {"q30_fraction": 0.934},
        "alignment": {"metrics": {"mean_depth": 42.7}},
        "variant_calling": {"filtered_vcf_path": str(vcf_path)},
    }
    (work_dir / "checkpoint.json").write_text(json.dumps(checkpoint))
    print("FAKE KIM: mode=%s -> %s" % (args.mode, vcf_path))
    sys.exit(0)
    """
)

FAKE_KIM_MAIN_FAILS = textwrap.dedent(
    """
    import sys
    print("FAKE KIM: simulated failure", file=sys.stderr)
    sys.exit(1)
    """
)

FAKE_GEPER_MAIN = textwrap.dedent(
    """
    import argparse, json
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("--vcf", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--blast-mode", default=None)
    p.add_argument("--blast-db", default=None)
    p.add_argument("--blast-reference-fasta", default=None)
    p.add_argument("--ai-only", action="store_true")
    p.add_argument("--species", default="human")
    p.add_argument("--assembly", default=None)
    p.add_argument("--max-variants", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--hpo-terms", default=None)
    p.add_argument("--phenotype-file", default=None)
    p.add_argument("--qc-metrics-json", default=None)
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "geper_results.json").write_text(
        json.dumps({"source_vcf": args.vcf, "variants": [], "qc_metrics_json": args.qc_metrics_json})
    )
    (out / "geper_report.md").write_text("# GEPER Clinical Report\\nSource VCF: %s\\n" % args.vcf)
    print("FAKE GEPER: processed %s" % args.vcf)
    """
)


def _write(path: Path, content: str) -> None:
    path.write_text(content)


class TestKimStage:
    def test_run_kim_returns_filtered_vcf(self, tmp_path):
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN)

        vcf = run_kim_fastq_to_vcf(
            fastq_r1="r1.fastq",
            reference_fasta="ref.fasta",
            output_dir=str(tmp_path / "kim_out"),
            sample_id="S01",
            kim_root=kim_root,
        )
        assert Path(vcf).exists()
        assert Path(vcf).name == "filtered_variants.vcf"

    def test_run_kim_propagates_failure(self, tmp_path):
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN_FAILS)

        with pytest.raises(BridgeError):
            run_kim_fastq_to_vcf(
                fastq_r1="r1.fastq",
                reference_fasta="ref.fasta",
                output_dir=str(tmp_path / "kim_out"),
                sample_id="S02",
                kim_root=kim_root,
            )

    def test_missing_kim_entry_point_raises(self, tmp_path):
        kim_root = tmp_path / "no_kim_here"
        kim_root.mkdir()
        with pytest.raises(BridgeError):
            run_kim_fastq_to_vcf(
                fastq_r1="r1.fastq",
                reference_fasta="ref.fasta",
                output_dir=str(tmp_path / "kim_out"),
                sample_id="S03",
                kim_root=kim_root,
            )


class TestGeperStage:
    def test_run_geper_returns_output_paths(self, tmp_path):
        geper_root = tmp_path / "geper"
        geper_root.mkdir()
        _write(geper_root / "main.py", FAKE_GEPER_MAIN)

        vcf_path = tmp_path / "filtered_variants.vcf"
        vcf_path.write_text("##fileformat=VCFv4.2\n")

        out = run_geper_vcf_to_report(
            vcf_path=str(vcf_path),
            output_dir=str(tmp_path / "geper_out"),
            geper_root=geper_root,
        )
        assert out["geper_results_json"] is not None
        assert out["geper_report_md"] is not None
        data = json.loads(Path(out["geper_results_json"]).read_text())
        assert data["source_vcf"] == str(vcf_path)


class TestCombinedWorkflow:
    def test_full_combined_workflow(self, tmp_path):
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN)

        geper_root = tmp_path / "geper"
        geper_root.mkdir()
        _write(geper_root / "main.py", FAKE_GEPER_MAIN)

        result = run_combined(
            fastq_r1="r1.fastq",
            reference_fasta="ref.fasta",
            kim_output_dir=str(tmp_path / "kim_out"),
            geper_output_dir=str(tmp_path / "geper_out"),
            sample_id="S_COMBINED",
            kim_root=kim_root,
            geper_root=geper_root,
        )

        assert result.success is True
        assert Path(result.filtered_vcf_path).exists()
        assert result.geper_results_json is not None
        assert result.geper_report_md is not None

        report_text = Path(result.geper_report_md).read_text()
        assert result.filtered_vcf_path in report_text

    def test_qc_metrics_sidecar_is_written_and_threaded_to_geper(self, tmp_path):
        """report review round 7: kim's real checkpoint values must
        reach GEPER's --qc-metrics-json, not stay stranded in Kim's own
        output directory."""
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN)

        geper_root = tmp_path / "geper"
        geper_root.mkdir()
        _write(geper_root / "main.py", FAKE_GEPER_MAIN)

        result = run_combined(
            fastq_r1="r1.fastq",
            reference_fasta="ref.fasta",
            kim_output_dir=str(tmp_path / "kim_out"),
            geper_output_dir=str(tmp_path / "geper_out"),
            sample_id="S_QC",
            kim_root=kim_root,
            geper_root=geper_root,
        )
        assert result.success is True

        sidecar_path = tmp_path / "kim_out" / "S_QC" / "qc_metrics.json"
        assert sidecar_path.exists()
        sidecar = json.loads(sidecar_path.read_text())
        assert sidecar["mean_coverage_depth"] == {"status": "found", "value": 42.7, "reason": None}
        assert sidecar["q30_score"] == {"status": "found", "value": 93.4, "reason": None}
        assert sidecar["bases_at_20x"]["status"] == "not_run"
        assert "mosdepth" in sidecar["bases_at_20x"]["reason"] or "samtools depth" in sidecar["bases_at_20x"]["reason"]

        geper_results = json.loads(Path(result.geper_results_json).read_text())
        assert geper_results["qc_metrics_json"] == str(sidecar_path)

    def test_combined_workflow_stops_if_kim_fails(self, tmp_path):
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN_FAILS)

        geper_root = tmp_path / "geper"
        geper_root.mkdir()
        _write(geper_root / "main.py", FAKE_GEPER_MAIN)

        with pytest.raises(BridgeError) as exc_info:
            run_combined(
                fastq_r1="r1.fastq",
                reference_fasta="ref.fasta",
                kim_output_dir=str(tmp_path / "kim_out"),
                geper_output_dir=str(tmp_path / "geper_out"),
                sample_id="S_FAIL",
                kim_root=kim_root,
                geper_root=geper_root,
            )
        assert exc_info.value.stage == "kim"
        # GEPER output must never have been created since Kim failed first.
        assert not (tmp_path / "geper_out" / "geper_results.json").exists()


class TestRelativePathResolution:
    """Regression test for: 'Expected checkpoint not found: work/kim/.../
    checkpoint.json' when the bridge is invoked with relative paths from
    a cwd other than kim_root/geper_root.

    Kim/GEPER subprocesses run with cwd=kim_root/geper_root (by design --
    see combined_pipeline.py module docstring), so a *relative*
    --output-dir is created relative to that root, not relative to
    wherever the bridge process itself was started. Before the fix, the
    bridge re-checked that same relative path against its own (different)
    cwd and never found it. This test chdirs somewhere unrelated to both
    kim_root and geper_root, passes relative --r1/--ref/--output-dir
    values, and asserts the whole workflow still succeeds.
    """

    def test_relative_paths_resolve_against_bridge_cwd_not_project_root(self, tmp_path, monkeypatch):
        kim_root = tmp_path / "kim_pipeline"
        kim_root.mkdir()
        _write(kim_root / "main.py", FAKE_KIM_MAIN)

        geper_root = tmp_path / "geper"
        geper_root.mkdir()
        _write(geper_root / "main.py", FAKE_GEPER_MAIN)

        # The bridge is invoked from a directory that is NOT kim_root and
        # NOT geper_root -- e.g. a repo root like /content/geper/integrated.
        invocation_dir = tmp_path / "wherever_the_user_ran_this_from"
        invocation_dir.mkdir()
        (invocation_dir / "r1.fastq").write_text("@read\nACGT\n+\nIIII\n")
        (invocation_dir / "ref.fasta").write_text(">chr1\nACGT\n")
        monkeypatch.chdir(invocation_dir)

        result = run_combined(
            fastq_r1="r1.fastq",  # relative, relative to invocation_dir
            reference_fasta="ref.fasta",  # relative, relative to invocation_dir
            kim_output_dir="work/kim",  # relative -- this is the exact case that failed
            geper_output_dir="work/geper",  # relative
            sample_id="TEST01",
            kim_root=kim_root,
            geper_root=geper_root,
        )

        assert result.success is True
        # The checkpoint/VCF must be discoverable regardless of which
        # process's cwd wrote them.
        assert Path(result.filtered_vcf_path).exists()
        assert Path(result.filtered_vcf_path).is_absolute()
        assert Path(result.geper_results_json).exists()
        assert Path(result.geper_report_md).exists()
        # And they must live under the *invocation* directory's "work/",
        # matching normal CLI expectations for a relative --output-dir --
        # not silently relocated under kim_root/geper_root.
        assert str(invocation_dir / "work" / "kim") in result.filtered_vcf_path
        assert str(invocation_dir / "work" / "geper") in result.geper_results_json


class TestQcMetricsFromKimCheckpoint:
    """
    report review round 7: `checkpoint["qc_r1"]` is written under the
    SAME key by two different kim_pipeline stages -- Stage 1
    (`fastq_validation`, no `q30_fraction`) always runs; Stage 1b (`qc`,
    the real QCStage, has `q30_fraction`) can silently fail to overwrite
    it if QCStage raises past its own generic-`except Exception`
    handler. A missing `q30_fraction` key is therefore ambiguous on its
    own -- these tests pin down that `qc_metrics_from_kim_checkpoint`
    resolves the ambiguity via `completed_stages`, not via key presence.
    """

    def test_both_stages_completed_with_real_values_are_found(self):
        checkpoint = {
            "completed_stages": ["fastq_validation", "qc", "alignment", "variant_calling"],
            "qc_r1": {"q30_fraction": 0.912},
            "alignment": {"metrics": {"mean_depth": 35.6}},
        }
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["mean_coverage_depth"] == {"status": "found", "value": 35.6, "reason": None}
        assert metrics["q30_score"] == {"status": "found", "value": 91.2, "reason": None}

    def test_q30_fraction_is_converted_from_fraction_to_percent(self):
        checkpoint = {
            "completed_stages": ["qc", "alignment"],
            "qc_r1": {"q30_fraction": 1.0},
            "alignment": {"metrics": {"mean_depth": 10.0}},
        }
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["q30_score"]["value"] == 100.0

    def test_bases_at_20x_is_always_not_run(self):
        checkpoint = {
            "completed_stages": ["fastq_validation", "qc", "alignment", "variant_calling"],
            "qc_r1": {"q30_fraction": 0.9},
            "alignment": {"metrics": {"mean_depth": 30.0}},
        }
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["bases_at_20x"]["status"] == "not_run"
        assert metrics["bases_at_20x"]["value"] is None
        assert metrics["bases_at_20x"]["reason"]  # non-empty, names the missing tool step

    def test_qc_stage_not_in_completed_stages_is_error_not_not_run(self):
        """The exact landmine: 'qc' never completed (QCStage raised past
        its own generic-except handler), so checkpoint['qc_r1'] -- if
        present at all -- may still be Stage 1's FastqStats dict, which
        has no q30_fraction. Must render ERROR (an upstream step was
        genuinely attempted and expected), never NOT_RUN (which would
        misreport this as never having been applicable)."""
        checkpoint = {
            "completed_stages": ["fastq_validation", "alignment", "variant_calling"],  # no "qc"
            "qc_r1": {"path": "r1.fastq", "total_records": 1000},  # Stage 1's FastqStats shape
            "alignment": {"metrics": {"mean_depth": 30.0}},
        }
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["q30_score"]["status"] == "error"
        assert metrics["q30_score"]["value"] is None
        assert (
            "fastq_validation" in metrics["q30_score"]["reason"] or "did not complete" in metrics["q30_score"]["reason"]
        )

    def test_qc_stage_missing_key_even_though_completed_is_error(self):
        """'qc' IS in completed_stages but the key is somehow still
        unusable (malformed checkpoint) -- must not crash, must not
        fabricate a value, ERROR with a clear reason."""
        checkpoint = {"completed_stages": ["qc", "alignment"], "qc_r1": {}, "alignment": {"metrics": {}}}
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["q30_score"]["status"] == "error"
        assert metrics["mean_coverage_depth"]["status"] == "error"

    def test_alignment_stage_not_completed_is_error(self):
        checkpoint = {"completed_stages": ["fastq_validation", "qc"], "qc_r1": {"q30_fraction": 0.9}}
        metrics = qc_metrics_from_kim_checkpoint(checkpoint)
        assert metrics["mean_coverage_depth"]["status"] == "error"
        assert "did not complete" in metrics["mean_coverage_depth"]["reason"]

    def test_empty_checkpoint_is_all_error_except_bases_at_20x(self):
        metrics = qc_metrics_from_kim_checkpoint({})
        assert metrics["mean_coverage_depth"]["status"] == "error"
        assert metrics["q30_score"]["status"] == "error"
        assert metrics["bases_at_20x"]["status"] == "not_run"

    def test_write_qc_metrics_sidecar_writes_valid_json(self, tmp_path):
        checkpoint = {
            "completed_stages": ["qc", "alignment"],
            "qc_r1": {"q30_fraction": 0.88},
            "alignment": {"metrics": {"mean_depth": 25.0}},
        }
        out_path = tmp_path / "nested" / "qc_metrics.json"
        result_path = write_qc_metrics_sidecar(checkpoint, str(out_path))
        assert result_path == str(out_path)
        assert out_path.exists()
        written = json.loads(out_path.read_text())
        assert written["mean_coverage_depth"]["value"] == 25.0
