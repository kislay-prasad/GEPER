"""
pipeline/alignment/stage.py
─────────────────────────────
AlignmentStage — FASTQ + reference genome → aligned.sorted.bam (+ .bai)
+ alignment_metrics.json.

Naming follows the existing sibling stages in this package
(`pipeline/qc/stage.py:QCStage`, `pipeline/blast/stage.py:BLASTStage`,
`pipeline/rna_analysis/stage.py:RNAStage`) — `AlignmentStage` here.

Orchestration only: aligner selection and SAM production are delegated
to `bwa_runner.py` / `minimap2_runner.py`; BAM conversion, sorting,
indexing, and metrics computation are delegated to `bam_utils.py`. This
file doesn't itself shell out to bwa/minimap2/samtools — it composes
the modules that do.

No fallback substitution between aligners: if the caller asks for
`aligner="bwa"` and BWA isn't installed, this raises rather than
silently running minimap2 instead. `aligner="auto"` is the only mode
that picks for you (BWA-MEM preferred when both are present, since
that's the primary aligner in the spec), and that choice is recorded in
the result so it's never ambiguous which tool actually produced the BAM.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError

from . import bam_utils
from . import bwa_runner
from . import minimap2_runner

logger = logging.getLogger("geper.pipeline.alignment.stage")


@dataclass
class AlignmentResult:
    sample_id: str = ""
    aligner_used: str = ""
    reference_fasta: str = ""
    fastq_r1: str = ""
    fastq_r2: Optional[str] = None
    sorted_bam_path: str = ""
    markdup_bam_path: str = ""
    bai_path: str = ""
    metrics: bam_utils.AlignmentMetrics = field(default_factory=bam_utils.AlignmentMetrics)
    metrics_report_path: str = ""
    commands_run: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "sample_id": self.sample_id,
            "aligner_used": self.aligner_used,
            "reference_fasta": self.reference_fasta,
            "fastq_r1": self.fastq_r1,
            "fastq_r2": self.fastq_r2,
            "sorted_bam_path": self.sorted_bam_path,
            "markdup_bam_path": self.markdup_bam_path,
            "bai_path": self.bai_path,
            "metrics": self.metrics.to_dict(),
            "metrics_report_path": self.metrics_report_path,
            "elapsed_seconds": self.elapsed_seconds,
        }


class AlignmentStage:
    """
    Args (config, under cfg["alignment"]):
        aligner    "auto" | "bwa" | "minimap2"   (default "auto")
        threads    int                            (default 4)
        preset     minimap2 preset string          (default "sr")
        index_dir  Optional persistent directory for the BWA FM-index
                   (e.g. a Google Drive mount path). When set, the index
                   is built into/reused from this directory instead of
                   next to the reference FASTA, so a completed index
                   survives an ephemeral runtime (Colab, etc.) being
                   wiped between sessions, and is never rebuilt once
                   complete. Has no effect on minimap2 (it doesn't use
                   a persisted on-disk index in this pipeline).
                   Default: None (index lives next to the reference,
                   same behavior as before this option existed).
    """

    def __init__(self, cfg: Optional[Dict] = None):
        self._cfg = (cfg or {}).get("alignment", {}) or {}
        self._default_aligner = self._cfg.get("aligner", "auto")
        self._default_threads = int(self._cfg.get("threads", 4))
        self._default_preset = self._cfg.get("preset", "sr")
        self._index_dir = self._cfg.get("index_dir")

    def _resolve_aligner(self, requested: str) -> str:
        if requested == "bwa":
            if not bwa_runner.is_available():
                raise FastqPipelineError(
                    "aligner='bwa' requested but no BWA binary is installed.",
                    stage="alignment", tool="bwa",
                )
            return "bwa"
        if requested == "minimap2":
            if not minimap2_runner.is_available():
                raise FastqPipelineError(
                    "aligner='minimap2' requested but minimap2 is not installed.",
                    stage="alignment", tool="minimap2",
                )
            return "minimap2"
        if requested == "auto":
            if bwa_runner.is_available():
                return "bwa"
            if minimap2_runner.is_available():
                return "minimap2"
            raise FastqPipelineError(
                "aligner='auto' but neither BWA nor minimap2 is installed. "
                "Install one: 'apt install bwa' or 'apt install minimap2'.",
                stage="alignment", tool="bwa/minimap2",
            )
        raise FastqPipelineError(
            f"Unknown aligner {requested!r}; expected 'auto', 'bwa', or 'minimap2'.",
            stage="alignment",
        )

    def run(
        self,
        fastq_r1: str,
        reference_fasta: str,
        output_dir: str,
        fastq_r2: Optional[str] = None,
        sample_id: str = "SAMPLE",
        aligner: Optional[str] = None,
        threads: Optional[int] = None,
        preset: Optional[str] = None,
        keep_intermediate_sam: bool = False,
        index_dir: Optional[str] = None,
    ) -> AlignmentResult:
        t0 = time.time()
        aligner_choice = self._resolve_aligner(aligner or self._default_aligner)
        threads = threads if threads is not None else self._default_threads
        preset = preset or self._default_preset
        index_dir = index_dir if index_dir is not None else self._index_dir

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        raw_sam_path = str(out / "aligned.raw.sam")
        sorted_bam_path = str(out / "aligned.sorted.bam")
        markdup_bam_path = str(out / "aligned.markdup.bam")
        metrics_report_path = str(out / "alignment_metrics.json")

        logger.info(
            "[%s] AlignmentStage: aligner=%s threads=%d ref=%s",
            sample_id, aligner_choice, threads, reference_fasta,
        )

        if aligner_choice == "bwa":
            bwa_runner.run_bwa_mem(
                fastq_r1, fastq_r2, reference_fasta, raw_sam_path,
                sample_id=sample_id, threads=threads, index_dir=index_dir,
            )
        else:
            minimap2_runner.run_minimap2(
                fastq_r1, fastq_r2, reference_fasta, raw_sam_path,
                sample_id=sample_id, threads=threads, preset=preset,
            )

        bam_utils.sam_to_sorted_bam(raw_sam_path, sorted_bam_path, threads=threads)
        bam_utils.mark_duplicates(sorted_bam_path, markdup_bam_path, threads=threads)

        if not keep_intermediate_sam:
            Path(raw_sam_path).unlink(missing_ok=True)
            Path(sorted_bam_path).unlink(missing_ok=True)

        bai_path = bam_utils.index_bam(markdup_bam_path, threads=threads)

        metrics = bam_utils.compute_flagstat_metrics(markdup_bam_path)
        metrics.mean_depth = bam_utils.compute_mean_depth(markdup_bam_path)
        metrics = bam_utils.attach_baseq_mapq(markdup_bam_path, metrics)
        if metrics.total_reads > 0:
            metrics.duplicate_pct = round(100.0 * metrics.duplicate_reads / metrics.total_reads, 3)

        bam_utils.write_metrics_report(
            metrics, metrics_report_path,
            extra={
                "sample_id": sample_id,
                "aligner": aligner_choice,
                "reference_fasta": reference_fasta,
                "bam_path": markdup_bam_path,
            },
        )

        result = AlignmentResult(
            sample_id=sample_id,
            aligner_used=aligner_choice,
            reference_fasta=reference_fasta,
            fastq_r1=fastq_r1,
            fastq_r2=fastq_r2,
            sorted_bam_path=markdup_bam_path,
            markdup_bam_path=markdup_bam_path,
            bai_path=bai_path,
            metrics=metrics,
            metrics_report_path=metrics_report_path,
            elapsed_seconds=round(time.time() - t0, 3),
        )

        logger.info(
            "[%s] Alignment complete: %s mapped %.1f%% of %d reads in %.2fs",
            sample_id, aligner_choice, metrics.pct_mapped, metrics.total_reads,
            result.elapsed_seconds,
        )
        return result
