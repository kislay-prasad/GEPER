"""
pipeline/qc/stage.py
─────────────────────
Production-grade FASTQ Quality Control (QC) stage.

Metrics computed per file (streaming, no full load into RAM):
  - Total read count
  - Per-base Phred score statistics (mean, median, Q1, Q3, per-position profiles)
  - GC content (overall + per-read distribution)
  - Read length distribution (min, max, mean, N50)
  - Adapter contamination detection (Illumina TruSeq + Nextera + common kits)
  - N-content analysis (per-read N fraction + positions)
  - Duplicate read estimation (first-read hash sampling)
  - Low-quality read filtering statistics
  - Per-tile quality (Illumina Casava read-name parsing, best-effort)
  - QC summary report written as JSON + HTML

Threshold enforcement:
  QCThresholdError is raised when any metric violates a configured threshold.
  The pipeline runner catches this and halts gracefully with a warning.

Pure stdlib — no third-party bioinformatics libraries required.
Gzip-transparent: both plain .fastq and .fastq.gz are supported.

Usage::

    from pipeline.qc.stage import QCStage

    stage = QCStage(cfg={})
    result = stage.run(fastq_r1="sample_R1.fastq.gz", output_dir="/work/qc")

    # Raise on threshold violations:
    stage.check_thresholds(result)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("geper.pipeline.qc.stage")

# ─── Known adapter sequences (first 12 bp are used for detection) ─────────────

_ADAPTERS: Dict[str, str] = {
    "TruSeq_R1": "AGATCGGAAGAGC",  # Illumina TruSeq Read 1
    "TruSeq_R2": "AGATCGGAAGAGC",  # Illumina TruSeq Read 2
    "Nextera": "CTGTCTCTTATAC",  # Nextera transposase
    "SmallRNA": "TGGAATTCTCGG",  # Small RNA kit
    "BGI_R1": "AAGTCGGAGGCC",  # BGI Read 1
    "BGI_R2": "AAGTCGGATCGC",  # BGI Read 2
    "PolyA": "AAAAAAAAAAAA",  # Poly-A tail
}

_ADAPTER_SEED_LEN = 12  # compare only the first N bp for speed

# ─── Default QC thresholds ────────────────────────────────────────────────────

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "min_mean_quality": 20.0,  # mean Phred across all bases
    "max_n_fraction": 0.1,  # fraction of bases that are N
    "min_gc_fraction": 0.2,  # overall GC fraction lower bound
    "max_gc_fraction": 0.8,  # overall GC fraction upper bound
    "max_adapter_fraction": 0.5,  # fraction of reads with adapter hits
    "max_duplicate_fraction": 0.8,  # estimated duplicate rate upper bound
    "min_total_reads": 100,  # minimum reads to consider a file usable
    "min_read_length": 25,  # minimum mean read length
}


# ─── Error types ──────────────────────────────────────────────────────────────


class QCError(Exception):
    """General QC stage failure (file I/O, parse error, etc.)."""


class QCThresholdError(QCError):
    """Raised when one or more QC metrics violate configured thresholds.

    Attributes:
        violations: dict mapping metric name → (observed, threshold, direction)
    """

    def __init__(self, violations: Dict[str, Tuple]) -> None:
        lines = []
        for metric, (observed, threshold, direction) in violations.items():
            lines.append(
                f"  {metric}: observed={observed:.4g} "
                f"{'<' if direction == 'min' else '>'} threshold={threshold:.4g}"
            )
        super().__init__(f"QC threshold violations ({len(violations)}):\n" + "\n".join(lines))
        self.violations = violations


# ─── Result data model ────────────────────────────────────────────────────────


@dataclass
class PerBasePhredStats:
    """Per-position quality score statistics (1-indexed positions)."""

    mean_by_position: List[float] = field(default_factory=list)
    median_by_position: List[float] = field(default_factory=list)
    q1_by_position: List[float] = field(default_factory=list)
    q3_by_position: List[float] = field(default_factory=list)


@dataclass
class QCMetrics:
    """All QC metrics for a single FASTQ file."""

    # ── Basic counters ───────────────────────────────────────────────────
    total_reads: int = 0
    total_bases: int = 0

    # ── Read length ──────────────────────────────────────────────────────
    min_read_length: int = 0
    max_read_length: int = 0
    mean_read_length: float = 0.0
    median_read_length: float = 0.0
    n50_read_length: int = 0  # length L such that 50% of bases in reads ≥ L

    # ── Phred quality ────────────────────────────────────────────────────
    mean_quality: float = 0.0  # mean Phred across ALL bases
    q20_fraction: float = 0.0  # fraction of bases ≥ Q20
    q30_fraction: float = 0.0  # fraction of bases ≥ Q30
    per_base_phred: PerBasePhredStats = field(default_factory=PerBasePhredStats)

    # ── GC content ───────────────────────────────────────────────────────
    gc_fraction: float = 0.0  # overall GC / (GC + AT)
    gc_distribution: List[float] = field(default_factory=list)  # per-read GC %

    # ── N content ────────────────────────────────────────────────────────
    n_fraction: float = 0.0  # fraction of all bases that are N
    reads_with_n_fraction: float = 0.0  # fraction of reads containing ≥1 N

    # ── Adapters ─────────────────────────────────────────────────────────
    adapter_hits: Dict[str, int] = field(default_factory=dict)  # name → count
    adapter_contamination_fraction: float = 0.0  # fraction of reads with any hit

    # ── Duplicates (estimated) ───────────────────────────────────────────
    duplicate_fraction_estimate: float = 0.0  # based on first-N-read hashing

    # ── Filtered read counts ─────────────────────────────────────────────
    low_quality_reads: int = 0  # reads with mean Q < threshold
    too_short_reads: int = 0  # reads shorter than min_read_length
    high_n_reads: int = 0  # reads with N fraction > max_n_fraction

    # ── Tile quality (Illumina, best-effort) ─────────────────────────────
    tile_quality_warnings: List[str] = field(default_factory=list)

    # ── QC status ────────────────────────────────────────────────────────
    qc_pass: bool = True
    qc_warnings: List[str] = field(default_factory=list)
    qc_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


@dataclass
class QCResult:
    """Top-level QC stage output for one sample."""

    sample_id: str = ""
    fastq_r1: str = ""
    fastq_r2: Optional[str] = None
    metrics_r1: QCMetrics = field(default_factory=QCMetrics)
    metrics_r2: Optional[QCMetrics] = None
    report_json_path: str = ""
    report_html_path: str = ""
    elapsed_seconds: float = 0.0
    qc_passed: bool = True  # False if any file fails thresholds

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


# ─── Streaming FASTQ parser ───────────────────────────────────────────────────


def _open_fastq(path: str):
    """Return a file handle for plain or gzip-compressed FASTQ."""
    p = Path(path)
    if not p.exists():
        raise QCError(f"FASTQ file not found: {path}")
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="ascii", errors="replace")
    return open(path, "r", encoding="ascii", errors="replace")


def _iter_records(path: str) -> Iterator[Tuple[str, str, str]]:
    """Yield (header, sequence, quality) for every FASTQ record.

    Validates four-line block structure; raises QCError on truncation.
    """
    with _open_fastq(path) as fh:
        while True:
            header = fh.readline()
            if not header:
                break  # EOF
            seq = fh.readline().rstrip("\n")
            fh.readline()  # '+' separator line; consumed to advance the file position, value unused
            qual = fh.readline().rstrip("\n")
            if not qual:
                raise QCError(f"Truncated FASTQ record in {path!r}")
            if not header.startswith("@"):
                raise QCError(f"Expected '@' header, got: {header[:40]!r}")
            yield header.rstrip("\n"), seq, qual


# ─── Per-base quality accumulator ────────────────────────────────────────────


class _PositionAccumulator:
    """Accumulate quality scores by position across all reads."""

    def __init__(self) -> None:
        self._scores: defaultdict = defaultdict(list)

    def add(self, qual_str: str) -> None:
        for i, ch in enumerate(qual_str):
            self._scores[i].append(ord(ch) - 33)

    def summarise(self) -> PerBasePhredStats:
        if not self._scores:
            return PerBasePhredStats()
        max_pos = max(self._scores.keys()) + 1
        means, medians, q1s, q3s = [], [], [], []
        for pos in range(max_pos):
            scores = self._scores.get(pos, [])
            if scores:
                means.append(statistics.mean(scores))
                medians.append(statistics.median(scores))
                sorted_s = sorted(scores)
                n = len(sorted_s)
                q1s.append(sorted_s[n // 4])
                q3s.append(sorted_s[3 * n // 4])
            else:
                means.append(0.0)
                medians.append(0.0)
                q1s.append(0.0)
                q3s.append(0.0)
        return PerBasePhredStats(
            mean_by_position=means,
            median_by_position=medians,
            q1_by_position=q1s,
            q3_by_position=q3s,
        )


# ─── N50 helper ──────────────────────────────────────────────────────────────


def _compute_n50(lengths: List[int]) -> int:
    if not lengths:
        return 0
    total = sum(lengths)
    half = total / 2
    cumulative = 0
    for length in sorted(lengths, reverse=True):
        cumulative += length
        if cumulative >= half:
            return length
    return 0


# ─── Tile extraction helper ───────────────────────────────────────────────────


def _extract_tile(header: str) -> Optional[str]:
    """Extract Illumina tile identifier from CASAVA-format header.

    Format: @<instrument>:<run>:<flowcell>:<lane>:<tile>:<x>:<y> ...
    Returns tile string or None if header does not match.
    """
    # Casava 1.8+ format
    parts = header.lstrip("@").split(":")
    if len(parts) >= 5:
        return f"lane{parts[3]}_tile{parts[4]}"
    return None


# ─── Core per-file QC function ───────────────────────────────────────────────


def _compute_qc(
    path: str,
    thresholds: Dict[str, float],
    max_dup_sample: int = 100_000,
    max_perbase_reads: int = 50_000,
) -> QCMetrics:
    """Stream through a FASTQ file and compute all QC metrics.

    Args:
        path:              FASTQ path (plain or .gz).
        thresholds:        Dict of threshold values.
        max_dup_sample:    Max reads to hash for duplicate estimation.
        max_perbase_reads: Max reads to accumulate for per-base stats
                           (memory guard for very large files).

    Returns:
        Populated QCMetrics instance.
    """
    logger.info("QC: scanning %s", path)

    metrics = QCMetrics()

    # Adapters: pre-compute seeds
    adapter_seeds = {name: seq[:_ADAPTER_SEED_LEN] for name, seq in _ADAPTERS.items()}
    adapter_hits: Counter = Counter()
    reads_with_adapter = 0

    # Quality accumulators
    total_phred_sum: float = 0.0
    q20_count: int = 0
    q30_count: int = 0
    n_base_count: int = 0
    reads_with_n: int = 0
    gc_count: int = 0
    at_count: int = 0
    gc_per_read: List[float] = []

    # Length distribution
    lengths: List[int] = []

    # Duplicate hashing
    seen_hashes: set = set()
    dup_count: int = 0
    dup_sample_done: bool = False

    # Per-base accumulator (memory-bounded)
    pos_acc = _PositionAccumulator()
    perbase_reads_counted = 0

    # Tile quality
    tile_mean_q: defaultdict = defaultdict(list)

    # Per-threshold counters
    lq_thresh = thresholds.get("min_mean_quality", DEFAULT_THRESHOLDS["min_mean_quality"])
    n_thresh = thresholds.get("max_n_fraction", DEFAULT_THRESHOLDS["max_n_fraction"])
    min_len_thresh = int(thresholds.get("min_read_length", DEFAULT_THRESHOLDS["min_read_length"]))

    low_quality_reads = 0
    too_short_reads = 0
    high_n_reads = 0

    for header, seq, qual in _iter_records(path):
        if not seq:
            continue

        L = len(seq)
        metrics.total_reads += 1
        metrics.total_bases += L
        lengths.append(L)

        # ── Per-base Phred ─────────────────────────────────────────────
        phred_vals = [ord(c) - 33 for c in qual]
        if perbase_reads_counted < max_perbase_reads:
            pos_acc.add(qual)
            perbase_reads_counted += 1

        read_q_sum = sum(phred_vals)
        read_mean_q = read_q_sum / L if L else 0.0
        total_phred_sum += read_q_sum
        q20_count += sum(1 for v in phred_vals if v >= 20)
        q30_count += sum(1 for v in phred_vals if v >= 30)

        # ── GC content ────────────────────────────────────────────────
        seq_upper = seq.upper()
        gc = seq_upper.count("G") + seq_upper.count("C")
        at = seq_upper.count("A") + seq_upper.count("T")
        gc_count += gc
        at_count += at
        gc_per_read.append(gc / L if L else 0.0)

        # ── N content ─────────────────────────────────────────────────
        n_cnt = seq_upper.count("N")
        n_base_count += n_cnt
        if n_cnt > 0:
            reads_with_n += 1
        n_frac_read = n_cnt / L if L else 0.0

        # ── Per-read filter counts ─────────────────────────────────────
        if read_mean_q < lq_thresh:
            low_quality_reads += 1
        if L < min_len_thresh:
            too_short_reads += 1
        if n_frac_read > n_thresh:
            high_n_reads += 1

        # ── Adapter detection ─────────────────────────────────────────
        read_has_adapter = False
        for aname, seed in adapter_seeds.items():
            if seed in seq_upper:
                adapter_hits[aname] += 1
                read_has_adapter = True
        if read_has_adapter:
            reads_with_adapter += 1

        # ── Duplicate estimation (first-read hash sampling) ────────────
        if not dup_sample_done:
            h = hashlib.md5(seq.encode(), usedforsecurity=False).digest()
            if h in seen_hashes:
                dup_count += 1
            else:
                seen_hashes.add(h)
            if metrics.total_reads >= max_dup_sample:
                dup_sample_done = True

        # ── Tile quality ───────────────────────────────────────────────
        tile = _extract_tile(header)
        if tile:
            tile_mean_q[tile].append(read_mean_q)

    # ── Finalise metrics ──────────────────────────────────────────────────
    n = metrics.total_reads
    if n == 0:
        metrics.qc_pass = False
        metrics.qc_failures.append("No reads found in file")
        return metrics

    metrics.mean_quality = total_phred_sum / metrics.total_bases if metrics.total_bases else 0.0
    metrics.q20_fraction = q20_count / metrics.total_bases
    metrics.q30_fraction = q30_count / metrics.total_bases

    metrics.gc_fraction = gc_count / (gc_count + at_count) if (gc_count + at_count) else 0.0
    # Sample GC distribution (at most 1000 values to keep JSON small)
    step = max(1, len(gc_per_read) // 1000)
    metrics.gc_distribution = [round(v, 4) for v in gc_per_read[::step]]

    metrics.n_fraction = n_base_count / metrics.total_bases if metrics.total_bases else 0.0
    metrics.reads_with_n_fraction = reads_with_n / n

    metrics.min_read_length = min(lengths)
    metrics.max_read_length = max(lengths)
    metrics.mean_read_length = statistics.mean(lengths)
    metrics.median_read_length = statistics.median(lengths)
    metrics.n50_read_length = _compute_n50(lengths)

    metrics.per_base_phred = pos_acc.summarise()

    metrics.adapter_hits = dict(adapter_hits)
    metrics.adapter_contamination_fraction = reads_with_adapter / n

    sample_size = min(n, max_dup_sample)
    metrics.duplicate_fraction_estimate = dup_count / sample_size if sample_size else 0.0

    metrics.low_quality_reads = low_quality_reads
    metrics.too_short_reads = too_short_reads
    metrics.high_n_reads = high_n_reads

    # Tile warnings: tiles whose mean Q is more than 10 below global mean
    global_mean = metrics.mean_quality
    for tile, qs in tile_mean_q.items():
        tile_mean = statistics.mean(qs)
        if tile_mean < global_mean - 10:
            metrics.tile_quality_warnings.append(
                f"{tile}: mean Q={tile_mean:.1f} (global={global_mean:.1f})"
            )

    return metrics


# ─── QC report writers ────────────────────────────────────────────────────────


def _write_json_report(result: QCResult, out_path: Path) -> None:
    """Write the QC result as a JSON file."""
    out_path.write_text(json.dumps(result.to_dict(), indent=2))


# REG-branding-drift-qc-report-still-says-GEPER-research-pipeline: the
# disclaimer below used to read "...is generated by the GEPER Research
# Pipeline and is intended for research use only. It does not constitute
# clinical advice." -- asserting the whole PRODUCT's positioning as
# research-only. That positioning was superseded 2026-08-22 (EJ-01), which
# ratified an assisted-clinical-use statement instead (see
# geper/report/clinical_report_builder.py's RESEARCH_USE_DISCLAIMER and its
# own comment history for the full account).
#
# This is a claim-accuracy fix, not a naming one: this QC stage runs before
# variant calling and reports raw FASTQ read metrics only -- it never
# classifies or interprets a variant either way, so it does not need (and
# should not borrow) the ratified disclaimer's clinician/draft-classification
# language, which describes a downstream artefact this one is not. Corrected
# to state only what THIS specific report is (a pre-analytic QC artefact,
# not a clinical claim in either direction), rather than asserting the
# product's overall positioning here too -- that question belongs to the one
# shared disclaimer, not a second, independently-drifting copy of it. Does
# NOT touch the "GEPER QC Report" title/heading below: what to call the
# product is a separate, deferred question and out of scope for this fix.
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>GEPER QC Report — {sample_id}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; background: #fafafa; }}
  h1 {{ color: #1a4a8a; border-bottom: 2px solid #1a4a8a; padding-bottom: 8px; }}
  h2 {{ color: #2a6ab5; margin-top: 28px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
  th {{ background: #2a6ab5; color: #fff; padding: 6px 12px; text-align: left; }}
  td {{ border: 1px solid #ccc; padding: 5px 10px; }}
  tr:nth-child(even) td {{ background: #f0f5ff; }}
  .PASS {{ background: #d4edda; color: #155724; padding: 2px 8px; border-radius: 4px; font-weight: bold; }}
  .FAIL {{ background: #f8d7da; color: #721c24; padding: 2px 8px; border-radius: 4px; font-weight: bold; }}
  .WARN {{ background: #fff3cd; color: #856404; padding: 2px 8px; border-radius: 4px; font-weight: bold; }}
  .section {{ background: #fff; border: 1px solid #dce3ef; border-radius: 6px; padding: 16px; margin-bottom: 20px; }}
  .metric-name {{ font-weight: bold; width: 35%; }}
  .disclaimer {{ font-size: 0.8em; color: #888; margin-top: 40px; border-top: 1px solid #ccc; padding-top: 10px; }}
</style>
</head>
<body>
<h1>GEPER QC Report</h1>
<p><strong>Sample:</strong> {sample_id} &nbsp;|&nbsp;
   <strong>Generated:</strong> {timestamp} &nbsp;|&nbsp;
   <strong>Overall QC:</strong> <span class="{overall_class}">{overall_status}</span></p>
{r1_section}
{r2_section}
<div class="disclaimer">
  DISCLAIMER: This is a pre-analytic sequencing quality-control report describing raw
  FASTQ read metrics only; it does not classify or interpret any variant and does not by
  itself constitute clinical advice. Always validate results with certified diagnostic
  tools and qualified clinical professionals.
</div>
</body>
</html>
"""

_METRICS_SECTION = """\
<div class="section">
  <h2>{title}</h2>
  <p><strong>File:</strong> {filepath}</p>
  <table>
    <tr><th>Metric</th><th>Value</th><th>Status</th></tr>
    {rows}
  </table>
  {warnings_block}
  {failures_block}
</div>
"""


def _status_badge(pass_: bool, warn: bool = False) -> str:
    if not pass_:
        return '<span class="FAIL">FAIL</span>'
    if warn:
        return '<span class="WARN">WARN</span>'
    return '<span class="PASS">PASS</span>'


def _metrics_to_rows(m: QCMetrics, thresholds: Dict) -> str:
    rows = []

    def row(name: str, value: str, pass_: bool, warn: bool = False) -> str:
        badge = _status_badge(pass_, warn)
        return f"<tr><td class='metric-name'>{name}</td><td>{value}</td><td>{badge}</td></tr>"

    lq = thresholds.get("min_mean_quality", DEFAULT_THRESHOLDS["min_mean_quality"])
    rows.append(
        row(
            "Total Reads",
            f"{m.total_reads:,}",
            m.total_reads >= thresholds.get("min_total_reads", 100),
        )
    )
    rows.append(row("Total Bases", f"{m.total_bases:,}", True))
    rows.append(row("Mean Quality (Phred)", f"{m.mean_quality:.2f}", m.mean_quality >= lq))
    rows.append(
        row(
            "Q20 Fraction",
            f"{m.q20_fraction:.3f}",
            m.q20_fraction >= 0.7,
            warn=m.q20_fraction < 0.8,
        )
    )
    rows.append(
        row(
            "Q30 Fraction",
            f"{m.q30_fraction:.3f}",
            m.q30_fraction >= 0.5,
            warn=m.q30_fraction < 0.6,
        )
    )
    rows.append(
        row(
            "Mean Read Length",
            f"{m.mean_read_length:.1f} bp",
            m.mean_read_length >= thresholds.get("min_read_length", 25),
        )
    )
    rows.append(row("Read Length (min/max)", f"{m.min_read_length} / {m.max_read_length} bp", True))
    rows.append(row("N50 Read Length", f"{m.n50_read_length} bp", True))
    gc_ok = (
        thresholds.get("min_gc_fraction", 0.2)
        <= m.gc_fraction
        <= thresholds.get("max_gc_fraction", 0.8)
    )
    rows.append(row("GC Content", f"{m.gc_fraction * 100:.1f}%", gc_ok))
    n_ok = m.n_fraction <= thresholds.get("max_n_fraction", 0.1)
    rows.append(row("N Base Fraction", f"{m.n_fraction:.4f}", n_ok))
    rows.append(
        row("Reads with N", f"{m.reads_with_n_fraction:.4f}", m.reads_with_n_fraction < 0.1)
    )
    ada_ok = m.adapter_contamination_fraction <= thresholds.get("max_adapter_fraction", 0.5)
    rows.append(
        row(
            "Adapter Contamination",
            f"{m.adapter_contamination_fraction:.3f}",
            ada_ok,
            warn=m.adapter_contamination_fraction > 0.1,
        )
    )
    dup_ok = m.duplicate_fraction_estimate <= thresholds.get("max_duplicate_fraction", 0.8)
    rows.append(
        row(
            "Est. Duplicate Rate",
            f"{m.duplicate_fraction_estimate:.3f}",
            dup_ok,
            warn=m.duplicate_fraction_estimate > 0.5,
        )
    )
    rows.append(row("Low-Quality Reads Filtered", f"{m.low_quality_reads:,}", True))
    rows.append(row("Too-Short Reads", f"{m.too_short_reads:,}", True))
    rows.append(row("High-N Reads", f"{m.high_n_reads:,}", True))

    # Adapter breakdown
    if m.adapter_hits:
        for aname, cnt in sorted(m.adapter_hits.items(), key=lambda x: -x[1]):
            frac = cnt / m.total_reads if m.total_reads else 0
            rows.append(
                row(f"  Adapter: {aname}", f"{cnt:,} ({frac:.2%})", frac < 0.1, warn=frac >= 0.1)
            )

    return "\n    ".join(rows)


def _build_html(result: QCResult, thresholds: Dict) -> str:
    def section(title: str, filepath: str, m: QCMetrics) -> str:
        rows = _metrics_to_rows(m, thresholds)
        warnings_block = ""
        if m.qc_warnings:
            items = "".join(f"<li>{w}</li>" for w in m.qc_warnings)
            warnings_block = f"<p><strong>Warnings:</strong><ul>{items}</ul></p>"
        failures_block = ""
        if m.qc_failures:
            items = "".join(f"<li>{f}</li>" for f in m.qc_failures)
            failures_block = (
                f"<p style='color:#721c24'><strong>Failures:</strong><ul>{items}</ul></p>"
            )
        if m.tile_quality_warnings:
            tw_items = "".join(f"<li>{w}</li>" for w in m.tile_quality_warnings)
            warnings_block += f"<p><strong>Tile Quality Issues:</strong><ul>{tw_items}</ul></p>"
        return _METRICS_SECTION.format(
            title=title,
            filepath=filepath,
            rows=rows,
            warnings_block=warnings_block,
            failures_block=failures_block,
        )

    r1_sec = section("R1 Quality Metrics", result.fastq_r1, result.metrics_r1)
    r2_sec = ""
    if result.metrics_r2 is not None:
        r2_sec = section("R2 Quality Metrics", result.fastq_r2 or "", result.metrics_r2)

    overall = result.qc_passed
    return _HTML_TEMPLATE.format(
        sample_id=result.sample_id,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        overall_class="PASS" if overall else "FAIL",
        overall_status="PASS" if overall else "FAIL",
        r1_section=r1_sec,
        r2_section=r2_sec,
    )


# ─── QC Stage ─────────────────────────────────────────────────────────────────


class QCStage:
    """Production FASTQ Quality Control stage.

    Integrates into the PipelineRunner between FASTQ validation and alignment.

    Args:
        cfg: Configuration dict (from default.yaml or production.yaml).
             Relevant keys:
               qc.thresholds.*          — override default thresholds
               qc.max_dup_sample        — reads to hash for dup estimation
               qc.stop_on_failure       — True (default) raises QCThresholdError
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = cfg or {}
        qc_cfg = self._cfg.get("qc", {})
        self._thresholds: Dict[str, float] = {
            **DEFAULT_THRESHOLDS,
            **qc_cfg.get("thresholds", {}),
        }
        self._max_dup_sample: int = qc_cfg.get("max_dup_sample", 100_000)
        self._stop_on_failure: bool = qc_cfg.get("stop_on_failure", True)

    # ── Public API ─────────────────────────────────────────────────────────

    def run(
        self,
        fastq_r1: str,
        output_dir: str,
        fastq_r2: Optional[str] = None,
        sample_id: str = "SAMPLE",
    ) -> QCResult:
        """Run QC on one or two FASTQ files and write JSON + HTML reports.

        Args:
            fastq_r1:    R1 FASTQ path.
            output_dir:  Directory where reports are written.
            fastq_r2:    R2 FASTQ path (optional, for paired-end).
            sample_id:   Sample identifier string.

        Returns:
            QCResult with populated metrics and report paths.

        Raises:
            QCThresholdError: If stop_on_failure=True and thresholds are violated.
            QCError:          On file I/O or parse failures.
        """
        t0 = time.time()
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[%s] QC Stage: scanning R1 %s", sample_id, fastq_r1)
        m_r1 = _compute_qc(fastq_r1, self._thresholds, self._max_dup_sample)
        self._annotate_pass_fail(m_r1)

        m_r2: Optional[QCMetrics] = None
        if fastq_r2:
            logger.info("[%s] QC Stage: scanning R2 %s", sample_id, fastq_r2)
            m_r2 = _compute_qc(fastq_r2, self._thresholds, self._max_dup_sample)
            self._annotate_pass_fail(m_r2)

        overall_pass = m_r1.qc_pass and (m_r2.qc_pass if m_r2 else True)

        result = QCResult(
            sample_id=sample_id,
            fastq_r1=fastq_r1,
            fastq_r2=fastq_r2,
            metrics_r1=m_r1,
            metrics_r2=m_r2,
            elapsed_seconds=round(time.time() - t0, 2),
            qc_passed=overall_pass,
        )

        # Write reports
        json_path = out_dir / "qc_report.json"
        html_path = out_dir / "qc_report.html"
        _write_json_report(result, json_path)
        html_path.write_text(_build_html(result, self._thresholds), encoding="utf-8")
        result.report_json_path = str(json_path)
        result.report_html_path = str(html_path)

        logger.info(
            "[%s] QC Stage done in %.1fs — overall: %s",
            sample_id,
            result.elapsed_seconds,
            "PASS" if overall_pass else "FAIL",
        )

        if not overall_pass and self._stop_on_failure:
            violations = self._collect_violations(m_r1, m_r2)
            raise QCThresholdError(violations)

        return result

    def check_thresholds(self, result: QCResult) -> None:
        """Explicitly check a QCResult against thresholds.

        Raises QCThresholdError if any metric is out of range.
        Can be called independently of run() (e.g., from tests).
        """
        violations = self._collect_violations(result.metrics_r1, result.metrics_r2)
        if violations:
            raise QCThresholdError(violations)

    # ── Private helpers ────────────────────────────────────────────────────

    def _annotate_pass_fail(self, m: QCMetrics) -> None:
        """Fill m.qc_pass, m.qc_warnings, m.qc_failures in-place."""
        t = self._thresholds

        checks: List[Tuple[str, float, float, str]] = [
            # (label, observed, threshold, direction)
            ("Mean quality", m.mean_quality, t["min_mean_quality"], "min"),
            ("N fraction", m.n_fraction, t["max_n_fraction"], "max"),
            ("GC fraction (low)", m.gc_fraction, t["min_gc_fraction"], "min"),
            ("GC fraction (high)", m.gc_fraction, t["max_gc_fraction"], "max"),
            (
                "Adapter contamination",
                m.adapter_contamination_fraction,
                t["max_adapter_fraction"],
                "max",
            ),
            ("Duplicate rate", m.duplicate_fraction_estimate, t["max_duplicate_fraction"], "max"),
            ("Total reads", float(m.total_reads), t["min_total_reads"], "min"),
            ("Mean read length", m.mean_read_length, t["min_read_length"], "min"),
        ]

        all_pass = True
        for label, observed, threshold, direction in checks:
            if direction == "min" and observed < threshold:
                m.qc_failures.append(f"{label}: {observed:.4g} < {threshold:.4g} (threshold)")
                all_pass = False
            elif direction == "max" and observed > threshold:
                m.qc_failures.append(f"{label}: {observed:.4g} > {threshold:.4g} (threshold)")
                all_pass = False

        # Soft warnings
        if m.q20_fraction < 0.8:
            m.qc_warnings.append(f"Q20 fraction {m.q20_fraction:.3f} < 0.8")
        if m.adapter_contamination_fraction > 0.1:
            m.qc_warnings.append(
                f"Adapter contamination {m.adapter_contamination_fraction:.2%} > 10%"
            )
        if m.duplicate_fraction_estimate > 0.5:
            m.qc_warnings.append(
                f"Estimated duplicate rate {m.duplicate_fraction_estimate:.2%} > 50%"
            )
        if m.tile_quality_warnings:
            m.qc_warnings.append(f"{len(m.tile_quality_warnings)} tile(s) with degraded quality")

        m.qc_pass = all_pass

    def _collect_violations(
        self,
        m_r1: QCMetrics,
        m_r2: Optional[QCMetrics] = None,
    ) -> Dict[str, Tuple]:
        """Return dict of all threshold violations across both files."""
        violations: Dict[str, Tuple] = {}
        t = self._thresholds

        def _check(prefix: str, m: QCMetrics) -> None:
            pairs = [
                ("mean_quality", m.mean_quality, t["min_mean_quality"], "min"),
                ("n_fraction", m.n_fraction, t["max_n_fraction"], "max"),
                ("gc_fraction_low", m.gc_fraction, t["min_gc_fraction"], "min"),
                ("gc_fraction_high", m.gc_fraction, t["max_gc_fraction"], "max"),
                (
                    "adapter_fraction",
                    m.adapter_contamination_fraction,
                    t["max_adapter_fraction"],
                    "max",
                ),
                (
                    "duplicate_fraction",
                    m.duplicate_fraction_estimate,
                    t["max_duplicate_fraction"],
                    "max",
                ),
                ("total_reads", float(m.total_reads), t["min_total_reads"], "min"),
                ("mean_read_length", m.mean_read_length, t["min_read_length"], "min"),
            ]
            for key, observed, threshold, direction in pairs:
                fail = (direction == "min" and observed < threshold) or (
                    direction == "max" and observed > threshold
                )
                if fail:
                    violations[f"{prefix}.{key}"] = (observed, threshold, direction)

        _check("r1", m_r1)
        if m_r2:
            _check("r2", m_r2)
        return violations
