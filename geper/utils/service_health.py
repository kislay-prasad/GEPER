"""
Lightweight external-service health tracking for GEPER's CLI batch
pipeline.

Motivation (real run, 2026-08-08): IndiGenomes (clingen.igib.res.in)
timed out on 100% of ~20 variants in a run, costing ~90s per variant
(3 attempts x 30s) for a service that was clearly down the whole time;
Ensembl/ClinGen ERepo intermittently threw 500/502/503s and
read-timeouts throughout the same run. Every external client already
retries transient failures on its own (see the per-client `_request`/
`_post`/`_fetch` loops in `database/`, `annotation/indigenomes.py`,
`pipeline/pvs1/lookup.py`, `pipeline/models/mmsplice/service.py`,
`pipeline/sequence_context.py`, `pipeline/clingen/utils.py`,
`pipeline/functional_evidence/erepo_provider.py` and
`mavedb_provider.py`) -- this module does not replace or duplicate
that retry logic. It adds two things on top of it:

1. A one-time startup probe (`run_startup_checks`) that tells the user
   up front which services are reachable, before the first variant is
   processed.
2. A tiny shared registry (`HEALTH`) that a client's *existing* retry
   loop can consult in one line at the top of the loop
   (`HEALTH.is_offline("IndiGenomes")`) to skip straight to raising
   `ExternalAPIError` instead of burning a full multi-attempt retry
   budget on a service already confirmed dead -- and to feed the
   final run summary (`print_summary`).

Deliberately not a generic circuit breaker: a service confirmed
offline at startup is treated as offline for the rest of the run (no
re-probing mid-run, no half-open state). The motivating failure mode
(a service down for an entire run) makes that single verdict good
enough, and it keeps this module small.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

import requests

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


class ServiceStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


# (status, detail) returned by a probe function, e.g. (ServiceStatus.HEALTHY, "").
ProbeResult = Tuple[ServiceStatus, str]


@dataclass
class ServiceRecord:
    name: str
    startup_status: ServiceStatus = ServiceStatus.UNKNOWN
    startup_detail: str = ""
    latency_ms: Optional[float] = None
    failure_count: int = 0
    success_count: int = 0
    skipped_count: int = 0
    _skip_logged: bool = False


@dataclass
class ServiceCheck:
    """One startup probe: `name` as shown in the table, `probe` does the actual request."""

    name: str
    probe: Callable[[], ProbeResult]


def _http_probe(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[str] = None,
    headers: Optional[dict] = None,
) -> ProbeResult:
    """
    Generic GET/POST reachability probe used to build `ServiceCheck`s.
    Any response at all (even a 4xx) means the host is up and routing
    requests -- only 5xx counts as "degraded" and only a network-level
    failure (timeout, connection error) counts as "offline".
    """
    timeout = CONFIG.health_check.TIMEOUT_SECS
    try:
        response = requests.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
    except requests.Timeout:
        return ServiceStatus.OFFLINE, "Timeout"
    except requests.RequestException as exc:
        return ServiceStatus.OFFLINE, type(exc).__name__

    if 500 <= response.status_code < 600:
        return ServiceStatus.DEGRADED, f"{response.status_code} {response.reason}".strip()
    return ServiceStatus.HEALTHY, ""


class ServiceHealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, ServiceRecord] = {}
        self._checked = False

    def _get(self, name: str) -> ServiceRecord:
        with self._lock:
            if name not in self._records:
                self._records[name] = ServiceRecord(name=name)
            return self._records[name]

    # -- startup -----------------------------------------------------

    def run_startup_checks(self, checks: List[ServiceCheck]) -> None:
        """
        Probe every service in parallel (bounded by
        `CONFIG.health_check.TIMEOUT_SECS`, not the sum of all
        services) and print the status table. No-op if disabled via
        `CONFIG.health_check.ENABLED` / `GEPER_HEALTH_CHECK_ENABLED`.
        """
        if not checks:
            return
        if not CONFIG.health_check.ENABLED:
            logger.info("External service health check disabled (GEPER_HEALTH_CHECK_ENABLED=false); skipping.")
            return

        def _run_one(check: ServiceCheck) -> None:
            record = self._get(check.name)
            start = time.perf_counter()
            try:
                status, detail = check.probe()
            except Exception as exc:  # pragma: no cover - defensive, probes shouldn't raise
                status, detail = ServiceStatus.OFFLINE, f"{type(exc).__name__}: {exc}"
            elapsed_ms = (time.perf_counter() - start) * 1000
            with self._lock:
                record.startup_status = status
                record.startup_detail = detail
                record.latency_ms = elapsed_ms

        with ThreadPoolExecutor(max_workers=max(len(checks), 1)) as pool:
            list(pool.map(_run_one, checks))

        self._checked = True
        self._print_table(
            "External Service Status",
            [self._get(check.name) for check in checks],
            lambda r: self._format_startup_line(r),
        )

    @staticmethod
    def _format_startup_line(record: ServiceRecord) -> str:
        label = f"{record.name} ".ljust(22, ".")
        if record.startup_status == ServiceStatus.HEALTHY:
            latency = f"{record.latency_ms:.0f} ms" if record.latency_ms is not None else "?"
            return f"✓ {label} Online ({latency})"
        if record.startup_status == ServiceStatus.DEGRADED:
            return f"⚠ {label} Degraded ({record.startup_detail})"
        return f"✗ {label} Offline ({record.startup_detail or 'unreachable'})"

    def _print_table(self, title: str, records: List[ServiceRecord], line_fn) -> None:
        width = 50
        lines = ["=" * width, title, "=" * width]
        for record in records:
            lines.append(line_fn(record))
        lines.append("=" * width)
        for line in lines:
            logger.info(line)

    # -- runtime -------------------------------------------------------

    def is_offline(self, name: str) -> bool:
        return self._get(name).startup_status == ServiceStatus.OFFLINE

    def note_skip(self, name: str) -> None:
        """
        Call from a client's retry loop instead of retrying, once it
        has already checked `is_offline(name)`. Logs a single
        explanatory message the first time per service per run, then
        stays silent (still counting every subsequent skip) so
        offline services don't spam the log once per variant.
        """
        record = self._get(name)
        with self._lock:
            record.skipped_count += 1
            should_log = not record._skip_logged
            record._skip_logged = True
        if should_log:
            logger.warning(
                f"{name} was confirmed offline at startup; skipping retries for this service "
                "for the rest of this run (further occurrences will be counted, not logged individually)."
            )

    def note_failure(self, name: str) -> None:
        record = self._get(name)
        with self._lock:
            record.failure_count += 1

    def note_success(self, name: str) -> None:
        record = self._get(name)
        with self._lock:
            record.success_count += 1

    # -- final summary ---------------------------------------------------

    def print_summary(self) -> None:
        if not self._records:
            return
        width = 50
        lines = ["=" * width, "External Services Summary", "=" * width]
        for record in self._records.values():
            lines.append(f"{record.name}".ljust(22, ".") + " " + self._summary_status(record))
        lines.append("=" * width)
        for line in lines:
            logger.info(line)

    @staticmethod
    def _summary_status(record: ServiceRecord) -> str:
        if record.startup_status == ServiceStatus.OFFLINE:
            return f"Offline (Skipped x{record.skipped_count})" if record.skipped_count else "Offline"
        if record.failure_count == 0:
            return "Healthy"
        return f"Intermittent ({record.failure_count} failures)"

    def reset(self) -> None:
        """Test/CLI-run hook: clear all state for a fresh run."""
        with self._lock:
            self._records = {}
            self._checked = False


HEALTH = ServiceHealthRegistry()


def is_transient_http_error(exc: Exception) -> bool:
    """
    True if `exc` (as caught by a client's existing
    `except (requests.RequestException, ValueError)` retry loop) is
    worth retrying: network-level failures (timeout, connection error)
    and 5xx responses are transient; a 400/404 `HTTPError` is a
    definitive answer from the server ("bad request" / "not found")
    that retrying cannot fix, so callers should stop immediately
    instead of spending the rest of their retry budget on it.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code not in (400, 404)
    return True


def default_service_checks() -> List[ServiceCheck]:
    """
    The standard set of external services GEPER's pipeline talks to,
    read from `config.py` so a probe always targets the same
    endpoint/env-var override the corresponding client itself uses.
    """
    _CONFIG = CONFIG

    checks = [
        ServiceCheck(
            "Ensembl",
            lambda: _http_probe(
                "GET",
                f"{_CONFIG.api.ENSEMBL_REST_BASE.rstrip('/')}/info/ping",
                params={"content-type": "application/json"},
            ),
        ),
        ServiceCheck(
            "ClinVar",
            lambda: _http_probe(
                "GET",
                f"{_CONFIG.api.NCBI_EUTILS_BASE.rstrip('/')}/einfo.fcgi",
                params={"db": _CONFIG.api.CLINVAR_DB, "retmode": "json"},
            ),
        ),
        ServiceCheck(
            "dbSNP",
            lambda: _http_probe(
                "GET",
                f"{_CONFIG.api.NCBI_EUTILS_BASE.rstrip('/')}/einfo.fcgi",
                params={"db": _CONFIG.api.DBSNP_DB, "retmode": "json"},
            ),
        ),
    ]
    if _CONFIG.indigenomes.ENABLED:
        checks.append(
            ServiceCheck(
                "IndiGenomes",
                lambda: _http_probe(
                    "POST",
                    _CONFIG.indigenomes.ENDPOINT,
                    data='{"Name": "__geper_health_check__"}',
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ),
            )
        )
    if _CONFIG.functional_evidence.ENABLED and _CONFIG.functional_evidence.EREPO_ENABLED:
        checks.append(
            ServiceCheck(
                "ClinGen ERepo",
                lambda: _http_probe(
                    "GET",
                    f"{_CONFIG.functional_evidence.EREPO_API_ENDPOINT.rstrip('/')}/curations",
                    params={"gene": "__geper_health_check__"},
                ),
            )
        )
    if _CONFIG.functional_evidence.ENABLED and _CONFIG.functional_evidence.MAVEDB_ENABLED:
        checks.append(
            ServiceCheck(
                "MaveDB",
                lambda: _http_probe(
                    "GET",
                    f"{_CONFIG.functional_evidence.MAVEDB_API_ENDPOINT.rstrip('/')}/score-sets/",
                ),
            )
        )
    return checks
