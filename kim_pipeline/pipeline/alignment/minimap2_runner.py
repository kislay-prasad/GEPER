"""
pipeline/alignment/minimap2_runner.py
────────────────────────────────────────
Minimap2 alignment runner.

Minimap2 doesn't need an out-of-band index step for correctness (unlike
BWA) — it builds its index in-memory per invocation unless given a
prebuilt `.mmi`. We don't bother prebuilding `.mmi` here since these
runs are single-shot per sample; the in-memory index cost is the same
order as the alignment itself for the data sizes this stage targets.

Preset defaults to `sr` (short, accurate genomic reads — i.e. the
Illumina paired-end case this stage is built for). A different preset
can be passed in for other data types (e.g. `map-ont` for nanopore),
but this module makes no attempt to auto-detect read technology from
the FASTQ — that would be a guess dressed up as a feature.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError, _require, _run

logger = logging.getLogger("geper.pipeline.alignment.minimap2")


def is_available() -> bool:
    return shutil.which("minimap2") is not None


def run_minimap2(
    fastq_r1: str,
    fastq_r2: Optional[str],
    reference_fasta: str,
    output_sam_path: str,
    sample_id: str = "SAMPLE",
    threads: int = 4,
    preset: str = "sr",
) -> str:
    """Align `fastq_r1` (+ optional `fastq_r2`) to `reference_fasta` with
    Minimap2, writing the raw SAM to `output_sam_path`. Returns that path.

    Raises `FastqPipelineError` if minimap2 isn't installed, inputs are
    missing, or the subprocess fails.
    """
    if not Path(reference_fasta).exists():
        raise FastqPipelineError(
            f"Reference FASTA not found: {reference_fasta!r}",
            stage="alignment.minimap2",
        )
    if not Path(fastq_r1).exists():
        raise FastqPipelineError(f"FASTQ R1 not found: {fastq_r1!r}", stage="alignment.minimap2")
    if fastq_r2 and not Path(fastq_r2).exists():
        raise FastqPipelineError(f"FASTQ R2 not found: {fastq_r2!r}", stage="alignment.minimap2")

    minimap2 = _require("minimap2", "alignment.minimap2")

    rg = f"@RG\\tID:{sample_id}\\tSM:{sample_id}\\tPL:ILLUMINA"
    cmd: List[str] = [
        minimap2, "-ax", preset, "-t", str(max(1, threads)), "-R", rg,
        reference_fasta, fastq_r1,
    ]
    if fastq_r2:
        cmd.append(fastq_r2)
    cmd += ["-o", output_sam_path]

    Path(output_sam_path).parent.mkdir(parents=True, exist_ok=True)
    _run(cmd, stage="alignment.minimap2")

    if not Path(output_sam_path).exists() or Path(output_sam_path).stat().st_size == 0:
        raise FastqPipelineError(
            f"minimap2 reported success but produced no/empty SAM at {output_sam_path}",
            stage="alignment.minimap2",
        )
    logger.info("Minimap2 (preset=%s) alignment complete: %s", preset, output_sam_path)
    return output_sam_path
