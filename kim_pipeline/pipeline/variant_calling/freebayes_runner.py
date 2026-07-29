"""
pipeline/variant_calling/freebayes_runner.py
───────────────────────────────────────────────
FreeBayes variant calling runner.

FreeBayes calls SNPs and short INDELs by Bayesian haplotype-based
inference directly from a BAM + reference — no separate INDEL-calling
pass is needed, both come out of the same VCF.

Threading: FreeBayes itself is single-threaded per region. Real
parallelism requires the `freebayes-parallel` wrapper script plus
`fasta_generate_regions.py` (both ship alongside FreeBayes, not as
separate packages). If `threads > 1` and both are on PATH, this uses
that; otherwise it runs single-process FreeBayes and says so in the
logs rather than silently pretending threads>1 had an effect.

This module deliberately does NOT fall back to a different caller
(e.g. bcftools) if the `freebayes` binary is missing — the brief asks
for FreeBayes specifically, and substituting a different statistical
model behind the same function signature would misrepresent what
produced the VCF. It raises `FastqPipelineError` with install
instructions instead.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError, _require, _run

from pipeline.alignment import bam_utils

logger = logging.getLogger("geper.pipeline.variant_calling.freebayes")


def is_available() -> bool:
    return shutil.which("freebayes") is not None


def _ensure_fasta_index(reference_fasta: str) -> None:
    """FreeBayes requires a `.fai` index for the reference. Build it
    with `samtools faidx` if missing."""
    if Path(reference_fasta + ".fai").exists():
        return
    samtools = _require("samtools", "variant_calling.faidx")
    logger.info("Reference .fai missing for %s — building with samtools faidx", reference_fasta)
    _run([samtools, "faidx", reference_fasta], stage="variant_calling.faidx")


def _ensure_bam_index(bam_path: str, threads: int) -> None:
    if Path(bam_path + ".bai").exists():
        return
    logger.info("BAM index missing for %s — building with samtools index", bam_path)
    bam_utils.index_bam(bam_path, threads=threads)


def run_freebayes(
    bam_path: str,
    reference_fasta: str,
    output_vcf_path: str,
    sample_id: str = "SAMPLE",
    threads: int = 1,
    min_base_quality: int = 20,
    min_mapping_quality: int = 20,
    min_alternate_fraction: float = 0.2,
    min_alternate_count: int = 2,
    region_size: int = 100_000,
) -> str:
    """Call SNPs and INDELs on `bam_path` against `reference_fasta` with
    FreeBayes, writing a plain (uncompressed) VCF to `output_vcf_path`.
    Returns that path.

    Raises `FastqPipelineError` if FreeBayes (or, for parallel mode, its
    helper scripts) is missing, inputs are missing, or the subprocess
    fails — never writes a placeholder VCF.
    """
    if not Path(bam_path).exists():
        raise FastqPipelineError(f"BAM not found: {bam_path!r}", stage="variant_calling.freebayes")
    if not Path(reference_fasta).exists():
        raise FastqPipelineError(
            f"Reference FASTA not found: {reference_fasta!r}", stage="variant_calling.freebayes",
        )

    freebayes = _require("freebayes", "variant_calling.freebayes")
    _ensure_fasta_index(reference_fasta)
    _ensure_bam_index(bam_path, threads)

    Path(output_vcf_path).parent.mkdir(parents=True, exist_ok=True)

    base_args = [
        "-f", reference_fasta,
        "--min-base-quality", str(min_base_quality),
        "--min-mapping-quality", str(min_mapping_quality),
        "--min-alternate-fraction", str(min_alternate_fraction),
        "--min-alternate-count", str(min_alternate_count),
    ]

    use_parallel = (
        threads > 1
        and shutil.which("freebayes-parallel") is not None
        and shutil.which("fasta_generate_regions.py") is not None
    )

    if use_parallel:
        logger.info("Running freebayes-parallel with %d regions/thread groups", threads)
        regions_cmd = f"fasta_generate_regions.py {reference_fasta}.fai {region_size}"
        cmd: List[str] = (
            ["freebayes-parallel", f"<({regions_cmd})", str(threads)] + base_args + ["-b", bam_path]
        )
        shell_cmd = " ".join(cmd) + f" > {output_vcf_path}"
        _run(["bash", "-c", shell_cmd], stage="variant_calling.freebayes")
    else:
        if threads > 1:
            logger.warning(
                "threads=%d requested but freebayes-parallel/fasta_generate_regions.py "
                "not both on PATH — running single-process FreeBayes instead.",
                threads,
            )
        cmd = [freebayes] + base_args + ["-b", bam_path]
        result = _run(cmd, stage="variant_calling.freebayes")
        Path(output_vcf_path).write_text(result.stdout)

    if not Path(output_vcf_path).exists():
        raise FastqPipelineError(
            f"freebayes reported success but produced no VCF at {output_vcf_path}",
            stage="variant_calling.freebayes",
        )
    logger.info("FreeBayes calling complete: %s", output_vcf_path)
    return output_vcf_path
