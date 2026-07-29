"""
pipeline/alignment/bwa_runner.py
──────────────────────────────────
BWA-MEM alignment runner.

Prefers `bwa-mem2` (faster, drop-in compatible CLI) and falls back to
classic `bwa mem` if only that is installed. Auto-builds the BWA FM-index
(`.bwt`/`.pac`/`.sa`/`.amb`/`.ann`) for the reference if it's missing, so
this is usable directly against a plain reference FASTA — it does not
require the operator to have pre-run `bwa index` out of band.

Produces a raw (unsorted) SAM file. Sorting/indexing/metrics are
`bam_utils.py`'s job, not this module's — keeps this runner symmetric
with `minimap2_runner.py` and avoids duplicating samtools logic in two
places.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError, _require, _run
from pipeline.utils.reference_cache import ReferenceCacheError, ensure_bwa_index

logger = logging.getLogger("geper.pipeline.alignment.bwa")

_BWA_INDEX_SUFFIXES = (".bwt", ".pac", ".sa", ".amb", ".ann")


def is_available() -> bool:
    return shutil.which("bwa-mem2") is not None or shutil.which("bwa") is not None


def _resolve_binary() -> str:
    if shutil.which("bwa-mem2"):
        return "bwa-mem2"
    if shutil.which("bwa"):
        return "bwa"
    raise FastqPipelineError(
        "No BWA binary found. Install one of: 'apt install bwa' "
        "(classic BWA-MEM) or 'conda install -c bioconda bwa-mem2' "
        "(faster, AVX2-accelerated). Required for the bwa aligner path.",
        stage="alignment.bwa",
        tool="bwa/bwa-mem2",
    )


def _ensure_index(binary: str, reference_fasta: str, index_dir: Optional[str] = None) -> str:
    """Ensure the BWA FM-index for `reference_fasta` exists, building it
    only if necessary, and return the index prefix to align against.

    Delegates to `pipeline.utils.reference_cache.ensure_bwa_index`, which:
      - skips the build entirely (near-zero cost) when a complete,
        fingerprint-matched index already exists,
      - optionally builds into/reuses a persistent `index_dir` (e.g. a
        Google Drive mount) so the index survives an ephemeral runtime
        being wiped between sessions,
      - streams live progress instead of staying silent until the whole
        build finishes, and
      - never mistakes a truncated/partial index (e.g. left behind by a
        killed session) for a complete one.
    """
    try:
        return ensure_bwa_index(binary, reference_fasta, index_dir=index_dir)
    except ReferenceCacheError as exc:
        raise FastqPipelineError(str(exc), stage="alignment.bwa_index", tool=binary) from exc


def run_bwa_mem(
    fastq_r1: str,
    fastq_r2: Optional[str],
    reference_fasta: str,
    output_sam_path: str,
    sample_id: str = "SAMPLE",
    threads: int = 4,
    index_dir: Optional[str] = None,
) -> str:
    """Align `fastq_r1` (+ optional `fastq_r2`) to `reference_fasta` with
    BWA-MEM, writing the raw SAM to `output_sam_path`. Returns that path.

    Args:
        index_dir: Optional persistent directory for the BWA FM-index
            (e.g. a Google Drive mount path). When set, the index is
            built into (or reused from) this directory instead of next
            to `reference_fasta`, so it survives an ephemeral runtime
            being wiped between sessions, and is never rebuilt once
            complete. When ``None`` (default), behavior is unchanged
            from before: the index lives next to the reference file.

    Raises `FastqPipelineError` if no BWA binary is found, the reference
    is missing, or the alignment subprocess fails — never produces a
    partial/placeholder SAM on failure.
    """
    if not Path(reference_fasta).exists():
        raise FastqPipelineError(
            f"Reference FASTA not found: {reference_fasta!r}",
            stage="alignment.bwa",
        )
    if not Path(fastq_r1).exists():
        raise FastqPipelineError(f"FASTQ R1 not found: {fastq_r1!r}", stage="alignment.bwa")
    if fastq_r2 and not Path(fastq_r2).exists():
        raise FastqPipelineError(f"FASTQ R2 not found: {fastq_r2!r}", stage="alignment.bwa")

    binary = _resolve_binary()
    index_prefix = _ensure_index(binary, reference_fasta, index_dir=index_dir)

    rg = f"@RG\\tID:{sample_id}\\tSM:{sample_id}\\tPL:ILLUMINA"
    cmd: List[str] = [binary, "mem", "-t", str(max(1, threads)), "-R", rg, index_prefix, fastq_r1]
    if fastq_r2:
        cmd.append(fastq_r2)
    cmd += ["-o", output_sam_path]

    Path(output_sam_path).parent.mkdir(parents=True, exist_ok=True)
    _run(cmd, stage="alignment.bwa")

    if not Path(output_sam_path).exists() or Path(output_sam_path).stat().st_size == 0:
        raise FastqPipelineError(
            f"{binary} mem reported success but produced no/empty SAM at {output_sam_path}",
            stage="alignment.bwa",
        )
    logger.info("BWA-MEM (%s) alignment complete: %s", binary, output_sam_path)
    return output_sam_path
