"""
bridge/combined_pipeline.py
────────────────────────────
Core orchestration logic for the combined workflow:

    FASTQ
      -> Kim Pipeline            (QC -> Alignment -> Variant Calling)
      -> filtered_variants.vcf
      -> Current GEPER           (Annotation -> AI Models -> Databases
                                   -> Interpretation Engine -> Clinical Report)

Design constraints (deliberate, do not "fix"):
  * This is NOT a repository merge. `geper/` and `kim_pipeline/` are run
    as separate subprocesses, each using its own entry point exactly as
    it would be invoked standalone. No annotation/ACMG/ClinVar/AI-model
    logic is copied or reimplemented here.
  * Kim is invoked with `analyze --mode vcf_only`, so it stops right
    after Variant Calling. Its own VEP/annotation/ACMG/PGx/ancestry/
    reporting stages never run in the combined workflow, but the modules
    implementing them are completely untouched and still work when Kim
    is run standalone (mode="full", the default).
  * GEPER is invoked with its existing `--vcf` CLI flag -- the same
    entry point used when a user runs GEPER directly against a VCF.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _abs(path: Optional[str]) -> Optional[str]:
    """Resolve a user-supplied path against the bridge's own invocation
    directory, before it is handed to a subprocess run with a different
    `cwd` (kim_root or geper_root).

    Bug this fixes: Kim/GEPER are launched with `cwd=kim_root` /
    `cwd=geper_root` (see design note above -- each project must see the
    same relative-path behavior it would get run standalone from its own
    directory). But every path the *bridge* itself later re-reads on the
    caller's behalf (checkpoint.json, the resulting VCF, the output
    directory used to look for geper_results.json/geper_report.md) was
    still being interpreted relative to whatever directory the bridge
    process happened to be running in -- a different, unrelated cwd from
    kim_root/geper_root. A relative --output-dir like "work/kim" would
    then be created at "<kim_root>/work/kim" by Kim's subprocess, while
    the bridge went looking for it at "<bridge's own cwd>/work/kim",
    which usually doesn't exist -- hence "Expected checkpoint not found"
    even though Kim's own run had succeeded and the file was really there.
    Resolving every path to absolute *before* it crosses the subprocess
    boundary makes the outcome independent of any subprocess's `cwd`.
    """
    if path is None:
        return None
    return str(Path(path).expanduser().resolve())


class BridgeError(RuntimeError):
    """Raised when either stage of the combined workflow fails."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"[{stage}] {message}")


# ─── Paths ─────────────────────────────────────────────────────────────────

# bridge/ sits alongside geper/ and kim_pipeline/ in the integrated package.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GEPER_ROOT = _PACKAGE_ROOT / "geper"
DEFAULT_KIM_ROOT = _PACKAGE_ROOT / "kim_pipeline"


@dataclass
class CombinedPipelineResult:
    """Complete result of a FASTQ -> Clinical Report combined run."""

    sample_id: str = ""

    # Stage 1: Kim (FASTQ -> VCF)
    kim_work_dir: str = ""
    kim_returncode: int = 0
    filtered_vcf_path: str = ""

    # Stage 2: GEPER (VCF -> Clinical Report)
    geper_output_dir: str = ""
    geper_returncode: int = 0
    geper_results_json: Optional[str] = None
    geper_report_md: Optional[str] = None

    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "kim_work_dir": self.kim_work_dir,
            "kim_returncode": self.kim_returncode,
            "filtered_vcf_path": self.filtered_vcf_path,
            "geper_output_dir": self.geper_output_dir,
            "geper_returncode": self.geper_returncode,
            "geper_results_json": self.geper_results_json,
            "geper_report_md": self.geper_report_md,
            "success": self.success,
            "error": self.error,
        }


# ─── Stage 1: Kim (FASTQ -> filtered_variants.vcf) ─────────────────────────

def run_kim_fastq_to_vcf(
    fastq_r1: str,
    reference_fasta: str,
    output_dir: str,
    sample_id: str,
    fastq_r2: Optional[str] = None,
    kim_root: Path = DEFAULT_KIM_ROOT,
    kim_python: Optional[str] = None,
    kim_config: Optional[str] = None,
    no_resume: bool = False,
    extra_args: Optional[List[str]] = None,
) -> str:
    """Run Kim's own `main.py analyze --mode vcf_only` entry point in a
    subprocess and return the path to the resulting filtered_variants.vcf.

    Nothing about Kim's annotation/ACMG/PGx/ancestry/reporting modules is
    touched -- `--mode vcf_only` simply tells Kim's existing
    `PipelineRunner.run()` to stop after Variant Calling (Stage 1 of this
    integration), which Kim's own regression suite continues to verify.
    """
    kim_root = Path(kim_root).expanduser().resolve()
    if not (kim_root / "main.py").exists():
        raise BridgeError("kim", f"Kim entry point not found at {kim_root / 'main.py'}")

    # Resolve every filesystem path to absolute *before* it crosses into a
    # subprocess run with cwd=kim_root -- otherwise a relative path (e.g.
    # a relative --output-dir) gets created by Kim relative to kim_root,
    # while the bridge below would go looking for it relative to its own
    # (different) cwd. See _abs()'s docstring for the full failure mode.
    fastq_r1 = _abs(fastq_r1)
    reference_fasta = _abs(reference_fasta)
    output_dir = _abs(output_dir)
    fastq_r2 = _abs(fastq_r2)
    kim_config = _abs(kim_config)

    python_bin = kim_python or sys.executable
    cmd = [
        python_bin, "main.py", "analyze",
        "--r1", fastq_r1,
        "--ref", reference_fasta,
        "--output-dir", output_dir,
        "--sample-id", sample_id,
        "--mode", "vcf_only",
    ]
    if fastq_r2:
        cmd += ["--r2", fastq_r2]
    if kim_config:
        cmd += ["--config", kim_config]
    if no_resume:
        cmd += ["--no-resume"]
    if extra_args:
        cmd += list(extra_args)

    proc = subprocess.run(cmd, cwd=str(kim_root))
    if proc.returncode != 0:
        raise BridgeError(
            "kim", f"Kim pipeline exited with code {proc.returncode} (cmd={' '.join(cmd)})"
        )

    checkpoint_path = Path(output_dir) / sample_id / "checkpoint.json"
    if not checkpoint_path.exists():
        raise BridgeError("kim", f"Expected checkpoint not found: {checkpoint_path}")

    checkpoint = json.loads(checkpoint_path.read_text())
    vc = checkpoint.get("variant_calling") or {}
    filtered_vcf_path = vc.get("filtered_vcf_path")
    if not filtered_vcf_path or not Path(filtered_vcf_path).exists():
        raise BridgeError(
            "kim",
            f"Kim did not produce a usable filtered_variants.vcf "
            f"(checkpoint variant_calling={vc!r})",
        )
    return filtered_vcf_path


# ─── Stage 2: GEPER (filtered_variants.vcf -> Clinical Report) ─────────────

def run_geper_vcf_to_report(
    vcf_path: str,
    output_dir: str,
    geper_root: Path = DEFAULT_GEPER_ROOT,
    geper_python: Optional[str] = None,
    blast_mode: Optional[str] = None,
    blast_db: Optional[str] = None,
    blast_reference_fasta: Optional[str] = None,
    ai_only: bool = False,
    species: Optional[str] = None,
    assembly: Optional[str] = None,
    max_variants: Optional[int] = None,
    no_resume: bool = False,
    hpo_terms: Optional[str] = None,
    phenotype_file: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Run GEPER's own `main.py --vcf ...` entry point in a subprocess.

    This is the exact same entry point a user invokes when running GEPER
    directly against a VCF (Project A remains the sole, authoritative
    implementation for annotation, AI models, ClinVar/dbSNP/gnomAD/
    ClinGen/UniProt/InterPro/Pfam/AlphaFold DB, BLAST+, ACMG automation,
    confidence engine, prioritization, conflict resolution,
    explainability, and clinical report generation).
    """
    geper_root = Path(geper_root).expanduser().resolve()
    if not (geper_root / "main.py").exists():
        raise BridgeError("geper", f"GEPER entry point not found at {geper_root / 'main.py'}")

    # Same fix as run_kim_fastq_to_vcf: resolve to absolute before this
    # crosses into a subprocess run with cwd=geper_root, so the bridge's
    # own later checks against output_dir (results_json/report_md below)
    # agree with wherever GEPER actually wrote them.
    vcf_path = _abs(vcf_path)
    output_dir = _abs(output_dir)
    blast_db = _abs(blast_db)
    blast_reference_fasta = _abs(blast_reference_fasta)
    phenotype_file = _abs(phenotype_file)

    python_bin = geper_python or sys.executable
    cmd = [python_bin, "main.py", "--vcf", vcf_path, "--output-dir", output_dir]
    if blast_mode:
        cmd += ["--blast-mode", blast_mode]
    if blast_db:
        cmd += ["--blast-db", blast_db]
    if blast_reference_fasta:
        cmd += ["--blast-reference-fasta", blast_reference_fasta]
    if ai_only:
        cmd += ["--ai-only"]
    if species:
        cmd += ["--species", species]
    if assembly:
        cmd += ["--assembly", assembly]
    if max_variants:
        cmd += ["--max-variants", str(max_variants)]
    if no_resume:
        cmd += ["--no-resume"]
    if hpo_terms:
        cmd += ["--hpo-terms", hpo_terms]
    if phenotype_file:
        cmd += ["--phenotype-file", phenotype_file]
    if extra_args:
        cmd += list(extra_args)

    proc = subprocess.run(cmd, cwd=str(geper_root))
    if proc.returncode != 0:
        raise BridgeError(
            "geper", f"GEPER pipeline exited with code {proc.returncode} (cmd={' '.join(cmd)})"
        )

    results_json = Path(output_dir) / "geper_results.json"
    report_md = Path(output_dir) / "geper_report.md"
    return {
        "geper_results_json": str(results_json) if results_json.exists() else None,
        "geper_report_md": str(report_md) if report_md.exists() else None,
    }


# ─── Combined workflow ──────────────────────────────────────────────────────

def run_combined(
    fastq_r1: str,
    reference_fasta: str,
    kim_output_dir: str,
    geper_output_dir: str,
    sample_id: str,
    fastq_r2: Optional[str] = None,
    kim_root: Path = DEFAULT_KIM_ROOT,
    geper_root: Path = DEFAULT_GEPER_ROOT,
    kim_python: Optional[str] = None,
    geper_python: Optional[str] = None,
    kim_config: Optional[str] = None,
    no_resume: bool = False,
    blast_mode: Optional[str] = None,
    blast_db: Optional[str] = None,
    blast_reference_fasta: Optional[str] = None,
    ai_only: bool = False,
    species: Optional[str] = None,
    assembly: Optional[str] = None,
    max_variants: Optional[int] = None,
    hpo_terms: Optional[str] = None,
    phenotype_file: Optional[str] = None,
) -> CombinedPipelineResult:
    """Run the complete FASTQ -> Clinical Report workflow:

        FASTQ -> Kim Pipeline -> filtered_variants.vcf -> GEPER -> Report
    """
    result = CombinedPipelineResult(sample_id=sample_id)
    result.kim_work_dir = str(Path(_abs(kim_output_dir)) / sample_id)
    result.geper_output_dir = _abs(geper_output_dir)

    try:
        filtered_vcf = run_kim_fastq_to_vcf(
            fastq_r1=fastq_r1,
            reference_fasta=reference_fasta,
            output_dir=kim_output_dir,
            sample_id=sample_id,
            fastq_r2=fastq_r2,
            kim_root=kim_root,
            kim_python=kim_python,
            kim_config=kim_config,
            no_resume=no_resume,
        )
        result.filtered_vcf_path = filtered_vcf

        geper_out = run_geper_vcf_to_report(
            vcf_path=filtered_vcf,
            output_dir=geper_output_dir,
            geper_root=geper_root,
            geper_python=geper_python,
            blast_mode=blast_mode,
            blast_db=blast_db,
            blast_reference_fasta=blast_reference_fasta,
            ai_only=ai_only,
            species=species,
            assembly=assembly,
            max_variants=max_variants,
            no_resume=no_resume,
            hpo_terms=hpo_terms,
            phenotype_file=phenotype_file,
        )
        result.geper_results_json = geper_out["geper_results_json"]
        result.geper_report_md = geper_out["geper_report_md"]
        result.success = True

    except BridgeError as exc:
        result.success = False
        result.error = str(exc)
        raise

    return result
