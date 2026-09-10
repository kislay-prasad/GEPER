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
    after Variant Calling. Its own VEP/annotation/ACMG/ancestry/
    reporting stages never run in the combined workflow, but the modules
    implementing them are completely untouched and still work when Kim
    is run standalone (mode="full", the default).
    PGx IS NO LONGER IN THAT LIST, AND ITS ABSENCE IS NOT AN OVERSIGHT:
    `kim_pipeline/pipeline/pgx/` was DELETED on 2026-09-10 (merge
    51320e1) when the human ruled pharmacogenomics off the clinical
    report and the computation removed with it. The guarantee above is
    unchanged for the four stages it still names; there is simply no
    PGx module left for it to be true or false of. The bridge never
    imported it -- this is a correction to what this file promises, not
    a change to what it does.
  * GEPER is invoked with its existing `--vcf` CLI flag -- the same
    entry point used when a user runs GEPER directly against a VCF.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
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


# ─── QC-metrics translation (kim_pipeline checkpoint.json -> GEPER's
#      {status, value, reason} sidecar shape) ──────────────────────────────
#
# Deliberately lives here, not in either project: GEPER must not know
# kim_pipeline's internal field names/units (checkpoint schema, fraction
# vs percent), and kim_pipeline has no reason to know GEPER's report
# schema. Translating between the two projects' own JSON shapes is
# exactly what this bridge module already exists to do (see its own
# docstring's "no annotation/ACMG/... logic is copied or reimplemented
# here" -- this is schema/unit translation, not evidence logic).

# kim_pipeline has no coverage-breadth (>=Nx) computation anywhere --
# confirmed by tracing pipeline/alignment/bam_utils.py (samtools
# coverage's own "coverage" column is "% positions with depth>0", a
# different question) and grepping for mosdepth/genomecov/20x, all
# empty. This is a permanent NOT_RUN, not a TODO -- see
# ROUND_CANDIDATES.md for the decision this round deliberately did not
# make (add the step, or drop the row).
_BASES_AT_20X_NOT_RUN_REASON = (
    "kim_pipeline does not currently compute bases-at->=20x coverage breadth for any sample -- no "
    "'samtools depth -a' threshold count or 'mosdepth --thresholds 20' step exists in "
    "pipeline/alignment/ (see ROUND_CANDIDATES.md)."
)


def qc_metrics_from_kim_checkpoint(checkpoint: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Translates one sample's kim_pipeline `checkpoint.json` into the
    `{"status": "found"|"not_run"|"error", "value": float|None,
    "reason": str|None}`-per-metric shape
    `report/summary.py::_parse_qc_metrics` requires. Pure function of
    the checkpoint dict -- no I/O, independently testable.

    The `completed_stages` guard below is the fix for a real ambiguity
    in kim_pipeline's own checkpoint schema (report review round 7):
    `checkpoint["qc_r1"]` is written under the SAME key by two
    different stages -- Stage 1 (`fastq_validation`, always runs,
    `FastqStats.to_dict()`, no `q30_fraction` field at all) and Stage 1b
    (`qc`, the real `QCStage`, `QCMetrics.to_dict()`, has
    `q30_fraction`). Stage 1b's own generic `except Exception` handler
    (kim_pipeline/pipeline/orchestration/runner.py) can log a warning
    and continue WITHOUT ever reaching the `checkpoint["qc_r1"] = ...`
    assignment -- so a missing `q30_fraction` key is ambiguous on its
    own: it could mean "Stage 1b hasn't run yet" or "Stage 1b ran and
    failed," and checkpoint["qc_r1"] would silently still hold Stage
    1's older, incompatible dict either way. Checking `"qc" in
    completed_stages` (not just whether the key happens to be present)
    is what tells these apart. This function does NOT change
    kim_pipeline's own error handling (that generic-`except Exception`
    is a separate, deliberately untouched decision -- see
    ROUND_CANDIDATES.md); it only refuses to guess past the ambiguity
    that handler creates.

    A combined-workflow run always attempts both `qc` and `alignment`
    (unlike GEPER's own VCF-only invocation, which has no "not
    applicable" state to fall back to here) -- so anything that didn't
    complete or didn't produce a usable number is reported `ERROR`, on
    the theory that a combined run promised an upstream pipeline ran
    and something specifically went wrong, never silently downgraded to
    `NOT_RUN` (which would misrepresent "this was expected and
    attempted" as "this was never applicable").
    """
    completed = set(checkpoint.get("completed_stages") or [])
    metrics: Dict[str, Dict[str, Any]] = {}

    # -- mean_coverage_depth: alignment stage, samtools coverage --------
    if "alignment" not in completed:
        metrics["mean_coverage_depth"] = {
            "status": "error",
            "value": None,
            "reason": "kim_pipeline's alignment stage did not complete this run (not recorded in "
            "checkpoint['completed_stages']).",
        }
    else:
        mean_depth = ((checkpoint.get("alignment") or {}).get("metrics") or {}).get("mean_depth")
        if isinstance(mean_depth, (int, float)) and not isinstance(mean_depth, bool):
            metrics["mean_coverage_depth"] = {"status": "found", "value": float(mean_depth), "reason": None}
        else:
            metrics["mean_coverage_depth"] = {
                "status": "error",
                "value": None,
                "reason": "kim_pipeline's alignment stage completed but reported no usable mean_depth "
                "(samtools coverage may have found zero covered bases).",
            }

    # -- q30_score: QC stage ONLY, never the fastq_validation stage -----
    if "qc" not in completed:
        metrics["q30_score"] = {
            "status": "error",
            "value": None,
            "reason": "kim_pipeline's QC stage did not complete this run (not recorded in "
            "checkpoint['completed_stages']) -- checkpoint['qc_r1'], if present at all, is from the "
            "simpler fastq_validation stage instead, which does not compute Q30.",
        }
    else:
        q30_fraction = (checkpoint.get("qc_r1") or {}).get("q30_fraction")
        if isinstance(q30_fraction, (int, float)) and not isinstance(q30_fraction, bool):
            metrics["q30_score"] = {
                "status": "found",
                "value": round(float(q30_fraction) * 100.0, 3),
                "reason": None,
            }
        else:
            metrics["q30_score"] = {
                "status": "error",
                "value": None,
                "reason": "kim_pipeline's QC stage completed but checkpoint['qc_r1'] carried no usable q30_fraction.",
            }

    # -- bases_at_20x: no computation exists anywhere in kim_pipeline ---
    metrics["bases_at_20x"] = {"status": "not_run", "value": None, "reason": _BASES_AT_20X_NOT_RUN_REASON}

    return metrics


def write_qc_metrics_sidecar(checkpoint: Dict[str, Any], output_path: str) -> str:
    """Writes `qc_metrics_from_kim_checkpoint(checkpoint)` as JSON to
    `output_path` and returns that path. `output_path`'s parent
    directory is created if needed."""
    metrics = qc_metrics_from_kim_checkpoint(checkpoint)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2))
    return str(out)


# ─── Reference-build forwarding (kim_pipeline checkpoint -> GEPER
#      --assembly) ──────────────────────────────────────────────────────────
#
# Report review round 8, Task C. Investigated first (this is NOT a
# dropped-field bug the way MaveDB provenance/QC metrics were): before
# this round, nothing anywhere computed an assembly value for a
# combined-workflow run to lose in the first place -- kim's own
# genome-build detection (`pipeline/utils/genome_build.py`) existed but
# was positioned after `--mode vcf_only`'s early return in
# `runner.py`, so it never even ran for the bridge's workflow. That
# reordering fix is what makes `checkpoint["detected_genome_build"]`
# exist at all; this function just reads it.


def _detected_assembly_from_kim_checkpoint(kim_output_dir: str, sample_id: str) -> Optional[str]:
    """
    Reads `checkpoint["detected_genome_build"]["build"]` (see
    `runner.py`'s genome-build-detection reordering fix) for one
    sample, if present and non-null. Returns `None` -- never a
    fabricated default -- when the checkpoint doesn't exist, doesn't
    have the key (an older kim_pipeline checkpoint, or a run that never
    reached this stage), or the detection itself came back
    undetermined (e.g. a single-contig VCF with no ##reference/##contig
    markers to detect from, the common case for a mitochondrial-only
    VCF -- rCRS numbering is build-invariant anyway, so "undetermined"
    is often the honest answer there, not a gap).
    """
    kim_dir_abs = _abs(kim_output_dir)
    if kim_dir_abs is None:
        return None
    checkpoint_path = Path(kim_dir_abs) / sample_id / "checkpoint.json"
    if not checkpoint_path.exists():
        return None
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, ValueError):
        return None
    detected = checkpoint.get("detected_genome_build") or {}
    return detected.get("build") or None


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

    Nothing about Kim's annotation/ACMG/ancestry/reporting modules is
    touched -- `--mode vcf_only` simply tells Kim's existing
    `PipelineRunner.run()` to stop after Variant Calling (Stage 1 of this
    integration), which Kim's own regression suite continues to verify.
    PGx used to be named here too. It was deleted on 2026-09-10 (merge
    51320e1), not quietly dropped from this sentence -- see the module
    docstring at the top of this file.
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
        python_bin,
        "main.py",
        "analyze",
        "--r1",
        fastq_r1,
        "--ref",
        reference_fasta,
        "--output-dir",
        output_dir,
        "--sample-id",
        sample_id,
        "--mode",
        "vcf_only",
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
        raise BridgeError("kim", f"Kim pipeline exited with code {proc.returncode} (cmd={' '.join(cmd)})")

    checkpoint_path = Path(output_dir) / sample_id / "checkpoint.json"
    if not checkpoint_path.exists():
        raise BridgeError("kim", f"Expected checkpoint not found: {checkpoint_path}")

    checkpoint = json.loads(checkpoint_path.read_text())
    vc = checkpoint.get("variant_calling") or {}
    filtered_vcf_path = vc.get("filtered_vcf_path")
    if not filtered_vcf_path or not Path(filtered_vcf_path).exists():
        raise BridgeError(
            "kim",
            f"Kim did not produce a usable filtered_variants.vcf (checkpoint variant_calling={vc!r})",
        )

    # QC-metrics sidecar for GEPER's Sequencing Quality Control Metrics
    # table (see `qc_metrics_from_kim_checkpoint`'s docstring). Written
    # unconditionally alongside checkpoint.json -- same discoverable-by-
    # path-convention pattern this function already uses for
    # checkpoint.json itself, so `run_combined` below can locate it
    # without `run_kim_fastq_to_vcf`'s own return type changing. Never
    # fatal: a translation bug here must not fail an otherwise-
    # successful Kim run any more than a PDF rendering bug is allowed to
    # fail an otherwise-successful GEPER run (report/orchestrator.py's
    # own convention for additive output).
    try:
        write_qc_metrics_sidecar(checkpoint, str(Path(output_dir) / sample_id / "qc_metrics.json"))
    except Exception as exc:  # noqa: BLE001 - additive output, must never fail an otherwise-successful Kim run
        print(f"WARNING: could not write qc_metrics.json sidecar ({exc}); GEPER's QC table will render ERROR rows.")

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
    qc_metrics_json: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Run GEPER's own `main.py --vcf ...` entry point in a subprocess.

    This is the exact same entry point a user invokes when running GEPER
    directly against a VCF (Project A remains the sole, authoritative
    implementation for annotation, AI models, ClinVar/dbSNP/gnomAD/
    ClinGen/UniProt/InterPro/Pfam/AlphaFold DB, BLAST+, ACMG automation,
    confidence engine, prioritization, conflict resolution,
    explainability, and clinical report generation).

    `qc_metrics_json`: path to the sidecar `run_kim_fastq_to_vcf` wrote
    (see `qc_metrics_from_kim_checkpoint`) -- always supplied by
    `run_combined` below when Kim actually ran, never a separate
    caller-facing override (a combined run has exactly one source of
    truth for its own QC metrics: Kim's own checkpoint, not a second
    value someone could pass that disagrees with it).
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
    qc_metrics_json = _abs(qc_metrics_json)

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
    if qc_metrics_json:
        cmd += ["--qc-metrics-json", qc_metrics_json]
    if extra_args:
        cmd += list(extra_args)

    proc = subprocess.run(cmd, cwd=str(geper_root))
    if proc.returncode != 0:
        raise BridgeError("geper", f"GEPER pipeline exited with code {proc.returncode} (cmd={' '.join(cmd)})")

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

        # Written by run_kim_fastq_to_vcf as a side effect, next to its
        # own checkpoint.json, using the same discoverable-by-path-
        # convention pattern -- see that function's docstring. Passed
        # through unconditionally (even if the write failed and the
        # file doesn't exist): GEPER's own `--qc-metrics-json` handling
        # already treats a missing file as "not applicable", never a
        # crash, so there is no need for run_combined to duplicate that
        # existence check here.
        qc_metrics_json = str(Path(_abs(kim_output_dir)) / sample_id / "qc_metrics.json")

        # Report review round 8, Task C: forward kim's own
        # `checkpoint["detected_genome_build"]` (see runner.py's
        # reordering fix) to GEPER's existing `--assembly` flag, ONLY
        # when the caller didn't already supply one explicitly --
        # `assembly` is a human-authored, deliberate choice (and
        # GEPER's `validate_assembly` treats a mismatch between it and
        # what the VCF header itself declares as a hard error), so an
        # auto-detected value must never override it, only fill the gap
        # when nothing was said at all.
        resolved_assembly = assembly
        if not resolved_assembly:
            resolved_assembly = _detected_assembly_from_kim_checkpoint(kim_output_dir, sample_id)

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
            assembly=resolved_assembly,
            max_variants=max_variants,
            no_resume=no_resume,
            hpo_terms=hpo_terms,
            phenotype_file=phenotype_file,
            qc_metrics_json=qc_metrics_json,
        )
        result.geper_results_json = geper_out["geper_results_json"]
        result.geper_report_md = geper_out["geper_report_md"]
        result.success = True

    except BridgeError as exc:
        result.success = False
        result.error = str(exc)
        raise

    return result
