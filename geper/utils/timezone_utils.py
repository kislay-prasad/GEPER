"""
India Standard Time (UTC+5:30, no daylight saving) display helper.

GEPER's internal/stored timestamps (`report/json_builder.py`'s
`generated_at`, log timestamps, everything produced via
`datetime.now(timezone.utc)` throughout this codebase) stay in UTC --
this module exists solely to convert a UTC timestamp to IST for
human-facing display in reports intended for Indian hospitals (the
PDF's "Report Generated" field, the Markdown report's "Generated"
line), never to change what gets stored, logged, or used to derive an
ID (e.g. `report/summary.py::_derive_run_id`, which needs a stable
string, not a display).

A fixed UTC+5:30 offset is used deliberately, not
`zoneinfo.ZoneInfo("Asia/Kolkata")`: India has observed no daylight
saving time since 1945, so the offset never changes, and a fixed
`timezone` object needs no IANA tzdata database -- avoiding a real
portability gap on platforms where `zoneinfo` has nothing to look up
(Windows without the separate `tzdata` PyPI package, some minimal
container/Colab images).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_DEFAULT_FORMAT = "%Y-%m-%d %H:%M IST"


def format_ist(dt: datetime, fmt: str = _DEFAULT_FORMAT) -> str:
    """
    Format `dt` for human display in IST.

    A naive `dt` (no `tzinfo`) is assumed to already be UTC, matching
    every timestamp this codebase produces internally, rather than
    raising or silently interpreting it as the local system's
    timezone (which would be wrong on every machine that isn't itself
    set to UTC).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt)


def format_ist_from_iso(iso_timestamp: Optional[str], fmt: str = _DEFAULT_FORMAT) -> str:
    """
    Parse an ISO-8601 timestamp string (e.g. a JSON document's
    `generated_at`) and format it for IST display.

    Never raises: a missing or unparseable value returns a clear
    placeholder / the original string unchanged, rather than crashing
    report generation over a display nicety -- some existing test
    fixtures deliberately use a non-ISO placeholder like `"now"` for
    `generated_at`, which must still render (unconverted) rather than
    blow up here.
    """
    if not iso_timestamp:
        return "Not available"
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return str(iso_timestamp)
    return format_ist(dt, fmt)
