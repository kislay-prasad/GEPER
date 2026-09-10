"""
Lightweight stage-timing profiler for GEPER.

Goal: answer "where does the wall-clock time in a run actually go?"
without adding a heavyweight profiling dependency and without touching
any prediction/annotation logic. Every stage the orchestrator runs
(sequence context, each DNA/RNA/protein/AlphaMissense model, BLAST,
ClinVar, dbSNP) is wrapped in a `with profiler.timer("stage_name"):`
block; this module only records durations, it never reads or mutates
the result the wrapped block produces.

Thread-safe: BLAST prefetch and any other future concurrent stage can
record into the same StageProfiler instance from multiple threads
without corrupting counts (a plain `list.append` under a per-instance
lock is enough at GEPER's call volume; this is not a hot loop that
needs lock-free tricks).
"""

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional


@dataclass
class StageStats:
    stage: str
    count: int = 0
    total_secs: float = 0.0
    min_secs: float = float("inf")
    max_secs: float = 0.0

    @property
    def mean_secs(self) -> float:
        return self.total_secs / self.count if self.count else 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "stage": self.stage,
            "count": self.count,
            "total_secs": round(self.total_secs, 4),
            "mean_secs": round(self.mean_secs, 4),
            "min_secs": round(self.min_secs, 4) if self.count else 0.0,
            "max_secs": round(self.max_secs, 4),
        }


class StageProfiler:
    """
    Accumulates wall-clock durations per named stage.

    Usage:
        profiler = StageProfiler()
        with profiler.timer("blast"):
            do_blast_call()
        ...
        print(profiler.to_markdown())
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._durations: Dict[str, List[float]] = {}
        self._run_started_at: Optional[float] = None
        self._run_ended_at: Optional[float] = None

    def mark_run_start(self) -> None:
        self._run_started_at = time.perf_counter()

    def mark_run_end(self) -> None:
        self._run_ended_at = time.perf_counter()

    @property
    def wall_clock_secs(self) -> Optional[float]:
        if self._run_started_at is None or self._run_ended_at is None:
            return None
        return self._run_ended_at - self._run_started_at

    @contextmanager
    def timer(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.record(stage, elapsed)

    def record(self, stage: str, elapsed_secs: float) -> None:
        with self._lock:
            self._durations.setdefault(stage, []).append(elapsed_secs)

    def stats(self) -> Dict[str, StageStats]:
        with self._lock:
            snapshot = {stage: list(values) for stage, values in self._durations.items()}
        result: Dict[str, StageStats] = {}
        for stage, values in snapshot.items():
            s = StageStats(stage=stage)
            s.count = len(values)
            s.total_secs = sum(values)
            s.min_secs = min(values)
            s.max_secs = max(values)
            result[stage] = s
        return result

    def total_secs_by_stage(self) -> Dict[str, float]:
        return {stage: s.total_secs for stage, s in self.stats().items()}

    def to_dict(self) -> Dict[str, object]:
        stats = self.stats()
        ordered = sorted(stats.values(), key=lambda s: s.total_secs, reverse=True)
        grand_total = sum(s.total_secs for s in ordered)
        return {
            "wall_clock_secs": round(self.wall_clock_secs, 4) if self.wall_clock_secs is not None else None,
            "sum_of_stage_secs": round(grand_total, 4),
            "stages": [
                {
                    **s.to_dict(),
                    "pct_of_stage_total": round(100.0 * s.total_secs / grand_total, 1) if grand_total else 0.0,
                }
                for s in ordered
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_markdown(self, title: str = "GEPER Performance Profile") -> str:
        data = self.to_dict()
        lines = [f"# {title}", ""]
        if data["wall_clock_secs"] is not None:
            lines.append(f"Total wall-clock time: **{data['wall_clock_secs']:.2f}s**")
            lines.append("")
        lines.append("| Stage | Calls | Total (s) | Mean (s) | Min (s) | Max (s) | % of measured time |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for row in data["stages"]:
            lines.append(
                f"| {row['stage']} | {row['count']} | {row['total_secs']:.3f} | "
                f"{row['mean_secs']:.3f} | {row['min_secs']:.3f} | {row['max_secs']:.3f} | "
                f"{row['pct_of_stage_total']:.1f}% |"
            )
        return "\n".join(lines)

    def log_summary(self, logger) -> None:
        """Emit a compact, glanceable summary via the given logger (INFO)."""
        stats = sorted(self.stats().values(), key=lambda s: s.total_secs, reverse=True)
        if not stats:
            return
        lines = ["", "===== GEPER Stage Timing Summary =====", ""]
        if self.wall_clock_secs is not None:
            lines.append(f"Total wall-clock: {self.wall_clock_secs:.2f}s")
        for s in stats:
            lines.append(
                f"  {s.stage:<28} calls={s.count:<6} total={s.total_secs:>9.3f}s "
                f"mean={s.mean_secs:>7.3f}s max={s.max_secs:>7.3f}s"
            )
        lines.append("=======================================")
        logger.info("\n".join(lines))
