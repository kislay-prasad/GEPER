"""
Regression tests for: `python main.py vcf` was only executing Annotation +
ACMG + bare EvidenceAggregator, silently skipping ClinVar, gnomAD, PGx, AI,
and the final report stage. Fixed by extracting the per-variant evidence
logic into pipeline/orchestration/shared.run_acmg_evidence_batch and wiring
it (plus PGx and ReportingStage) into main.cmd_vcf.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


SAMPLE_VCF = """##fileformat=VCFv4.2
##contig=<ID=chr17>
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr17\t43057051\t.\tA\tT\t100\tPASS\t.\tGT\t0/1
"""

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shared_module_exists_and_is_importable():
    """pipeline/orchestration/shared.py must exist and expose
    run_acmg_evidence_batch (used by both `analyze` and `vcf`)."""
    from pipeline.orchestration.shared import run_acmg_evidence_batch
    assert callable(run_acmg_evidence_batch)


def test_runner_uses_shared_function_not_inline_duplicate():
    """PipelineRunner must call the shared function rather than containing
    its own duplicate copy of the ClinVar/gnomAD/ACMG loop."""
    import inspect
    from pipeline.orchestration import runner as runner_mod
    src = inspect.getsource(runner_mod)
    assert "run_acmg_evidence_batch" in src
    # The inline per-variant loop construct should no longer appear directly
    # in runner.py (it was moved to shared.py).
    assert "for variant in ann_variants:" not in src


def test_cmd_vcf_wires_clinvar_gnomad_pgx_reporting(tmp_path):
    """End-to-end smoke test: `python main.py vcf` must execute (or attempt
    and gracefully report skipping) ClinVar, gnomAD, PGx, and the final
    report — not just annotation + ACMG."""
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(SAMPLE_VCF)
    out_dir = tmp_path / "out"

    proc = subprocess.run(
        [sys.executable, "main.py", "vcf", "--input", str(vcf), "--output-dir", str(out_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr

    # ClinVar/gnomAD/ACMG/PGx/reporting must all have been *attempted*
    # (network-restricted test environments may show them as non-fatal
    # warnings, but they must no longer be silently absent from the code path).
    assert "ClinVar" in combined or "clinvar" in combined
    assert "gnomAD" in combined or "gnomad" in combined
    assert "PGx" in combined or "pgx" in combined
    assert "Stages completed:" in combined

    classified = out_dir / "sample" / "classified_variants.json"
    assert classified.exists()
    data = json.loads(classified.read_text())
    assert len(data) == 1
