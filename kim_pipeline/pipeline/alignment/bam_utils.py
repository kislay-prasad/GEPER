"""
pipeline/alignment/bam_utils.py
────────────────────────────────
Shared BAM utilities for the alignment stage.

Both `bwa_runner.py` and `minimap2_runner.py` produce a raw SAM file;
everything downstream of "I have a SAM file" (BAM conversion, sorting,
indexing, and alignment metrics) lives here exactly once, so the two
aligner runners stay thin and don't duplicate samtools-invocation logic.

Reuses, rather than reimplements, the subprocess/error-handling
primitives already established in `geper/pipeline/fastq/pipeline.py`:
  - `FastqPipelineError` — same exception type the rest of GEPER's
    file-based tool-orchestration code raises.
  - `_require()` — resolves a tool on PATH or raises an actionable error.
  - `_run()` — runs a subprocess, raising `FastqPipelineError` with the
    captured stderr on non-zero exit.

No fallback substitution: if samtools is missing, this raises rather
than silently skipping sort/index/metrics steps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError, _require, _run

logger = logging.getLogger("geper.pipeline.alignment.bam_utils")


# ─── Result type ───────────────────────────────────────────────────────────────

@dataclass
class AlignmentMetrics:
    """Real metrics computed from the sorted BAM via samtools — never
    estimated or fabricated. Any field that couldn't be computed (e.g.
    insert size for single-end data) stays at its default and is not
    backfilled with a guess."""
    total_reads: int = 0
    mapped_reads: int = 0
    pct_mapped: float = 0.0
    secondary_reads: int = 0
    supplementary_reads: int = 0
    duplicate_reads: int = 0
    paired_reads: int = 0
    properly_paired_reads: int = 0
    pct_properly_paired: float = 0.0
    mean_depth: Optional[float] = None
    mean_baseq: Optional[float] = None
    mean_mapq: Optional[float] = None
    duplicate_pct: Optional[float] = None

    def to_dict(self) -> Dict:
        return asdict(self)


# ─── SAM → sorted, indexed BAM ──────────────────────────────────────────────────

def sam_to_sorted_bam(sam_path: str, output_bam_path: str, threads: int = 4) -> str:
    """Sort a SAM (or unsorted BAM) by coordinate into `output_bam_path`
    using `samtools sort`. Returns the output path."""
    samtools = _require("samtools", "alignment.sort")
    Path(output_bam_path).parent.mkdir(parents=True, exist_ok=True)
    _run(
        [samtools, "sort", "-@", str(max(1, threads)), "-o", output_bam_path, sam_path],
        stage="alignment.sort",
    )
    logger.info("Sorted BAM written: %s", output_bam_path)
    return output_bam_path


def mark_duplicates(sorted_bam_path: str, output_bam_path: str, threads: int = 4) -> str:
    """Mark duplicate reads in a sorted BAM using ``samtools markdup``.

    Parses the DUPLICATE TOTAL line from samtools markdup stderr and logs
    the number of duplicate reads flagged. Returns the output path.
    """
    samtools = _require("samtools", "alignment.markdup")
    Path(output_bam_path).parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [samtools, "markdup", "-@", str(max(1, threads)), sorted_bam_path, output_bam_path],
        stage="alignment.markdup",
    )
    # Parse duplicate count from stderr: line containing "DUPLICATE TOTAL"
    dup_total: Optional[int] = None
    for line in (result.stderr or "").splitlines():
        if "DUPLICATE TOTAL" in line:
            parts = line.split()
            try:
                dup_total = int(parts[-1])
            except (ValueError, IndexError):
                pass
            break
    if dup_total is not None:
        logger.info("Duplicate reads flagged: %d", dup_total)
    else:
        logger.info("Duplicate marking complete (could not parse DUPLICATE TOTAL from stderr)")
    return output_bam_path


def index_bam(bam_path: str, threads: int = 4) -> str:
    """Create a `.bai` index for `bam_path` via `samtools index`. Returns
    the index path. Safe to call even if an index already exists (it is
    regenerated)."""
    samtools = _require("samtools", "alignment.index")
    _run([samtools, "index", "-@", str(max(1, threads)), bam_path], stage="alignment.index")
    bai_path = bam_path + ".bai"
    if not Path(bai_path).exists():
        raise FastqPipelineError(
            f"samtools index reported success but {bai_path} was not created",
            stage="alignment.index",
        )
    logger.info("BAM index written: %s", bai_path)
    return bai_path


# ─── Metrics ────────────────────────────────────────────────────────────────────

def compute_flagstat_metrics(bam_path: str) -> AlignmentMetrics:
    """Parse `samtools flagstat -O tsv` into structured `AlignmentMetrics`.
    TSV mode is used (rather than scraping the human-readable text) so
    parsing doesn't depend on samtools' free-text phrasing."""
    samtools = _require("samtools", "alignment.flagstat")
    result = _run([samtools, "flagstat", "-O", "tsv", bam_path], stage="alignment.flagstat")

    m = AlignmentMetrics()
    rows: Dict[str, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        count_str, _qc_fail, label = parts[0], parts[1], parts[2]
        try:
            rows[label.strip()] = int(count_str)
        except ValueError:
            continue

    m.total_reads = rows.get("total (QC-passed reads + QC-failed reads)", 0)
    m.mapped_reads = rows.get("primary mapped", rows.get("mapped", 0))
    m.secondary_reads = rows.get("secondary", 0)
    m.supplementary_reads = rows.get("supplementary", 0)
    m.duplicate_reads = rows.get("duplicates", 0)
    m.paired_reads = rows.get("paired in sequencing", 0)
    m.properly_paired_reads = rows.get("properly paired", 0)

    primary_total = rows.get("primary", m.total_reads - m.secondary_reads - m.supplementary_reads)
    if primary_total > 0:
        m.pct_mapped = round(100.0 * m.mapped_reads / primary_total, 3)
    if m.paired_reads > 0:
        m.pct_properly_paired = round(100.0 * m.properly_paired_reads / m.paired_reads, 3)

    return m


def compute_mean_depth(bam_path: str) -> Optional[float]:
    """Genome/region-wide mean depth via `samtools coverage`, weighted by
    contig length. Returns None (not 0.0) if the BAM has zero covered
    bases — a real absence of signal, not a computed zero."""
    samtools = _require("samtools", "alignment.coverage")
    result = _run([samtools, "coverage", bam_path], stage="alignment.coverage")

    lines = [l for l in result.stdout.splitlines() if l and not l.startswith("#")]
    if not lines:
        return None

    total_len = 0
    weighted_depth = 0.0
    for line in lines:
        cols = line.split("\t")
        # samtools coverage columns: rname startpos endpos numreads covbases
        #                            coverage meandepth meanbaseq meanmapq
        if len(cols) < 7:
            continue
        try:
            start, end = int(cols[1]), int(cols[2])
            meandepth = float(cols[6])
        except ValueError:
            continue
        length = max(0, end - start + 1)
        total_len += length
        weighted_depth += meandepth * length

    if total_len == 0:
        return None
    return round(weighted_depth / total_len, 4)


def attach_baseq_mapq(bam_path: str, metrics: AlignmentMetrics) -> AlignmentMetrics:
    """Fill in mean base/mapping quality from `samtools coverage`'s
    per-contig columns, length-weighted, mirroring `compute_mean_depth`."""
    samtools = _require("samtools", "alignment.coverage")
    result = _run([samtools, "coverage", bam_path], stage="alignment.coverage")
    lines = [l for l in result.stdout.splitlines() if l and not l.startswith("#")]

    total_len = 0
    weighted_baseq = 0.0
    weighted_mapq = 0.0
    for line in lines:
        cols = line.split("\t")
        if len(cols) < 9:
            continue
        try:
            start, end = int(cols[1]), int(cols[2])
            meanbaseq, meanmapq = float(cols[7]), float(cols[8])
        except ValueError:
            continue
        length = max(0, end - start + 1)
        total_len += length
        weighted_baseq += meanbaseq * length
        weighted_mapq += meanmapq * length

    if total_len > 0:
        metrics.mean_baseq = round(weighted_baseq / total_len, 3)
        metrics.mean_mapq = round(weighted_mapq / total_len, 3)
    return metrics


def write_metrics_report(metrics: AlignmentMetrics, path: str, extra: Optional[Dict] = None) -> str:
    """Write the alignment metrics report as JSON. `extra` lets the
    caller (stage.py) attach run-level context (aligner, reference,
    sample_id, paths) without this module needing to know about them."""
    payload = metrics.to_dict()
    if extra:
        payload.update(extra)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2))
    logger.info("Alignment metrics report written: %s", path)
    return path
