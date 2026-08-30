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
   processed. It retries a network-level failure before reporting a
   service offline (`CONFIG.health_check.PROBE_ATTEMPTS`): because an
   offline verdict here switches off the per-client retry loops
   described above, it has to rest on more than one sample, or a single
   slow request silently disables the very machinery that would have
   ridden it out.
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

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    # False for the external data sources this registry was built for
    # (ClinVar, Ensembl, ...), True for an in-process component
    # registered here only to borrow the reporting path -- see
    # `note_component_failure`. The distinction is carried explicitly
    # rather than inferred from the counters, because the caveat this
    # feeds says two different things: an external source "failed and
    # was retried, so its evidence may be complete, partial or absent",
    # which is a false statement about a local detector that raised
    # once and was never retried at all.
    is_local_component: bool = False
    _skip_logged: bool = False
    # Retained so a latched service can be re-probed later. Two
    # consumers, both inside this class: the bounded re-probe
    # (`_maybe_reprobe`) during the run, and `measure_latched_services()`
    # at the end of it, which clears this field when it is done.
    #
    # Underscored to keep the surface narrow: this is not a
    # general-purpose "re-probe this service" handle for arbitrary
    # callers. That does not stop anyone determined -- nothing here does
    # -- but the re-probe policy lives in one place, and a client
    # reaching in to call this directly would bypass the attempt cap
    # that keeps a hard-down service from being polled all run.
    _probe: Optional[Callable[[], ProbeResult]] = None
    # `time.monotonic()` at the moment this service latched OFFLINE, so
    # the end-of-run measurement can report how long the latch stood.
    latched_at: Optional[float] = None
    # Bounded-reprobe bookkeeping. `skips_since_reprobe` resets on every
    # attempt so the skip trigger measures "skips since we last looked",
    # not "skips ever"; `last_reprobe_at` starts at the latch instant so
    # the first time-triggered attempt is measured from the latch.
    skips_since_reprobe: int = 0
    reprobe_attempts: int = 0
    last_reprobe_at: Optional[float] = None


@dataclass
class ServiceCheck:
    """One startup probe: `name` as shown in the table, `probe` does the actual request."""

    name: str
    probe: Callable[[], ProbeResult]


# ---------------------------------------------------------------------------
# Re-probe timing
#
# SIZED FROM DATA. Every real (non-mocked) healthy probe latency recorded
# since 5094620 landed: 2144, 2327, 2376, 2606, 2831 ms. 4.0s clears the
# slowest observed success by ~29%, so a service that has genuinely
# recovered answers inside it. Deliberately NOT
# `LATCH_CONFIRM_TIMEOUT_SECS` (30s): that long timeout exists to justify
# *latching*, where a false positive costs the whole run. Re-probing
# decides whether to *un-latch*, where being wrong costs one more skip
# window -- the cheap direction.
#
# STILL PENDING, NOT GUESSED HERE: the re-probe interval (N skips / T
# seconds) and the attempt cap. Those need latch-to-recovery durations,
# which no log can contain until something actually re-probes -- see
# `measure_latched_services()`, which exists to produce exactly that
# evidence. Do not fill these in from judgement; the numbers are days
# away, not unearnable.
# ---------------------------------------------------------------------------
_REPROBE_TIMEOUT_SECS = 4.0


def _under_pytest() -> bool:
    """
    True when this process is running the test suite.

    Health-probe log lines are tagged with the result so a later
    analysis can exclude them. This is not cosmetic: of the 213 latch
    events logged in the 14 hours after 5094620, effectively all came
    from pytest -- 96 of 101 `Online` readings were `0 ms` mocks and
    twelve read `RuntimeError: probe blew up`. Counting those as
    evidence of real service behaviour is how a parameter gets sized
    from fiction.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _ctx() -> str:
    """Log-line suffix marking synthetic (test-run) samples."""
    return " [pytest]" if _under_pytest() else ""


def _health_check_setting(name: str, default: float) -> float:
    """
    Read one `CONFIG.health_check` number, falling back to *default* if
    it is absent or not a real number.

    The fallback is not defensive padding: `CONFIG` is module-global here
    and is patched with a `mock.Mock()` in the tests, so any attribute
    added to `HealthCheckConfig` reads back as a Mock rather than a
    number in every test that predates it. Coercing those to the real
    default keeps this function's behaviour identical to the shipped
    config instead of raising TypeError deep inside a retry loop.
    """
    value = getattr(CONFIG.health_check, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


# Per-thread timeout override for the attempt currently in flight.
# `ServiceCheck.probe` is a zero-argument callable (tests supply plain
# Python functions), so the retry loop in `run_startup_checks` cannot
# pass the attempt's timeout down as a parameter without changing that
# signature for every existing caller. Each check already runs on its
# own pool thread, so a thread-local carries it cleanly instead.
_PROBE_STATE = threading.local()


def _current_probe_timeout() -> float:
    """Timeout for the probe attempt in flight on this thread."""
    return getattr(_PROBE_STATE, "timeout", None) or _health_check_setting("TIMEOUT_SECS", 4.0)


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

    One request only: retrying is `run_startup_checks`'s job, so that it
    applies to every `ServiceCheck` rather than only to the ones built
    from this helper.

    The timeout is `TIMEOUT_SECS` normally, but the attempt that will
    actually decide to latch uses the longer `LATCH_CONFIRM_TIMEOUT_SECS`
    (see `_current_probe_timeout`).
    """
    timeout = _current_probe_timeout()
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

        attempts = max(1, int(_health_check_setting("PROBE_ATTEMPTS", 3)))
        backoff = max(0.0, float(_health_check_setting("PROBE_BACKOFF_SECS", 0.5)))
        fast_timeout = float(_health_check_setting("TIMEOUT_SECS", 4.0))
        confirm_timeout = max(fast_timeout, float(_health_check_setting("LATCH_CONFIRM_TIMEOUT_SECS", 30.0)))

        def _run_one(check: ServiceCheck) -> None:
            record = self._get(check.name)
            # Retry before latching: an OFFLINE verdict here is treated as
            # CONFIRMED for the whole run and makes every client skip its
            # own retry loop, so it must not rest on a single sample. Only
            # OFFLINE is retried -- HEALTHY and DEGRADED are both real
            # answers from a host that is up and routing, and a 5xx in
            # particular is a definitive reply, not a missing one.
            status, detail = ServiceStatus.OFFLINE, "unreachable"
            elapsed_ms = 0.0
            for attempt in range(1, attempts + 1):
                is_final = attempt == attempts
                _PROBE_STATE.timeout = confirm_timeout if is_final else fast_timeout
                start = time.perf_counter()
                try:
                    status, detail = check.probe()
                except Exception as exc:  # pragma: no cover - defensive, probes shouldn't raise
                    status, detail = ServiceStatus.OFFLINE, f"{type(exc).__name__}: {exc}"
                finally:
                    # Timed per attempt, so the recorded latency describes
                    # the attempt whose verdict we keep rather than the sum
                    # of the failures that preceded it.
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    _PROBE_STATE.timeout = None
                if status != ServiceStatus.OFFLINE:
                    break
                if not is_final:
                    logger.debug(
                        f"{check.name} health probe failed ({detail}) on attempt {attempt}/{attempts}; "
                        f"retrying in {backoff:.1f}s before treating it as offline."
                    )
                    if backoff:
                        time.sleep(backoff)
            else:
                # Only reached when every attempt came back OFFLINE.
                logger.warning(
                    f"{check.name} health probe failed all {attempts} attempts (last: {detail}) "
                    f"after {elapsed_ms:.0f} ms; treating it as offline for the rest of this run -- "
                    f"clients will skip their own retries for this service.{_ctx()}"
                )
            with self._lock:
                record.startup_status = status
                record.startup_detail = detail
                record.latency_ms = elapsed_ms
                # Kept for the end-of-run measurement below. Storing the
                # callable does not make anything re-probe on its own.
                record._probe = check.probe
                record.latched_at = time.monotonic() if status == ServiceStatus.OFFLINE else None
                record.last_reprobe_at = record.latched_at

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
        # Latency is reported for OFFLINE too, not just HEALTHY. It was
        # already measured and then discarded, which left no way to tell a
        # fast-fail (connection refused, milliseconds) from a full timeout
        # -- the difference that decides what re-probing actually costs.
        # Appended AFTER the detail parentheses so the existing
        # "Offline (<detail>)" substring is unchanged.
        offline = f"✗ {label} Offline ({record.startup_detail or 'unreachable'})"
        if record.latency_ms is not None:
            offline += f" after {record.latency_ms:.0f} ms"
        return offline

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
        """
        True while `name` is latched offline for this run.

        This is also where the bounded re-probe fires. It is a query, so
        putting a network call behind it needs justifying: the
        alternative was to make `note_success()`/`note_failure()`
        load-bearing and recover on client traffic, which would silently
        disable recovery for any client that forgot to call one -- a new
        silent-failure mode in a module whose whole purpose is to stop
        one. Every caller that would benefit from recovery already calls
        `is_offline()` before skipping, so this is the one place the
        check is guaranteed to be reached without adding a call site.
        The cost is bounded by `REPROBE_MAX_ATTEMPTS` for the whole run.
        """
        record = self._get(name)
        if record.startup_status != ServiceStatus.OFFLINE:
            return False
        self._maybe_reprobe(record)
        return record.startup_status == ServiceStatus.OFFLINE

    def _maybe_reprobe(self, record: ServiceRecord) -> None:
        """
        Re-run a latched service's own startup probe, at most
        `REPROBE_MAX_ATTEMPTS` times per run, once either enough skips
        have accumulated or enough time has passed since the last
        attempt.

        The probe is called OUTSIDE the lock. Holding it across a
        network call would block every other service's `is_offline()`
        for the probe's full timeout, which is the opposite of what a
        module that exists to avoid waiting on dead services should do.
        The decision to attempt, and the application of the result, are
        both taken under the lock; only the call itself is outside it.
        """
        cfg = CONFIG.health_check
        now = time.monotonic()
        with self._lock:
            if record._probe is None or record.reprobe_attempts >= cfg.REPROBE_MAX_ATTEMPTS:
                return
            since = now - record.last_reprobe_at if record.last_reprobe_at is not None else 0.0
            due = record.skips_since_reprobe >= cfg.REPROBE_AFTER_SKIPS or since >= cfg.REPROBE_AFTER_SECS
            if not due:
                return
            # Claim the attempt before releasing the lock, so two
            # threads arriving together cannot both spend one.
            record.reprobe_attempts += 1
            record.skips_since_reprobe = 0
            record.last_reprobe_at = now
            probe = record._probe
            attempt = record.reprobe_attempts

        try:
            status, detail = probe()
        except Exception as exc:  # a probe that raises is a probe that failed
            status, detail = ServiceStatus.OFFLINE, f"{type(exc).__name__}: {exc}"

        with self._lock:
            if status != ServiceStatus.OFFLINE:
                record.startup_status = status
                record.startup_detail = detail
                record.latched_at = None
                # Deliberately keep `_probe`: nothing says a recovered
                # service cannot go down again, and the attempt counter
                # still bounds how often we look.
                logger.info(
                    f"{record.name} recovered: re-probe {attempt} of {cfg.REPROBE_MAX_ATTEMPTS} succeeded "
                    f"after {record.skipped_count} skipped call(s); this service is no longer being skipped."
                )
                return
            remaining = cfg.REPROBE_MAX_ATTEMPTS - attempt
            if remaining == 0:
                logger.info(
                    f"{record.name} is still offline after {attempt} re-probe(s); no further re-probes will "
                    "be attempted for this run."
                )

    def offline_services(self) -> List[str]:
        """
        Names of every service confirmed offline at startup for this
        run, in probe order. Read by the report layer (see
        `report/summary.py`/`report/summary_short.py`) to print a
        single run-level caveat distinguishing "this source was
        skipped this run" from "this source was queried and found
        nothing" -- the two must never look identical in a report a
        clinician might rely on.
        """
        with self._lock:
            return [record.name for record in self._records.values() if record.startup_status == ServiceStatus.OFFLINE]

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
            record.skips_since_reprobe += 1
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

    def note_component_failure(self, name: str) -> None:
        """
        Record that an IN-PROCESS component failed, so the failure
        reaches the report bundle by the same route a data-source
        failure does.

        Why this lives here rather than in a second surface: Finding 5
        established that a handled failure which never reaches the
        document is invisible to the reviewer -- `logger.exception` is
        not part of the output bundle. That reasoning does not care
        whether the thing that failed was across the network or in this
        process. What differs is only the sentence a reader should see,
        which is why the record is tagged `is_local_component` instead
        of being silently folded in with the retried data sources.
        """
        record = self._get(name)
        with self._lock:
            record.failure_count += 1
            record.is_local_component = True

    # -- final summary ---------------------------------------------------

    def measure_latched_services(self) -> None:
        """
        OBSERVATION ONLY. Re-probe every service that latched OFFLINE,
        log what came back, and change nothing.

        This is instrumentation, not the bounded-reprobe feature. It
        exists because that feature's parameters (re-probe interval,
        attempt cap) need latch-to-recovery durations, and no log can
        ever contain those while nothing re-probes: the evidence can
        only be produced by re-probing, so the gate could never clear on
        its own. This produces it without touching the runtime contract.

        The question it answers, once per run, per latched service: was
        the latch still true by the end of the run? A latch that has
        recovered is a false latch, and its age at recovery is the
        number the interval should be sized against.

        CALL THIS LAST. It consumes each latched service's retained probe
        and clears it, which is safe only because `main.py` calls it in
        its `finally`, after everything else. If the bounded-reprobe
        feature (shape (a)) is built later, it will re-probe through that
        same handle during the run -- calling this mid-run would clear
        the handle out from under it and silently disable recovery for
        the rest of that run. Nothing enforces the ordering; this comment
        is the enforcement.

        WHAT IT MUST NOT DO, and does not: un-latch, alter
        `startup_status` / `startup_detail` / `latency_ms`, touch the
        skip/failure/success counters, or influence `is_offline()`.
        Every caller sees exactly what it saw before this ran. The
        counters in particular are what `print_summary` reports, so
        writing to them here would silently corrupt the run summary.
        """
        with self._lock:
            latched = [r for r in self._records.values() if r.startup_status == ServiceStatus.OFFLINE and r._probe]
        if not latched:
            return

        for record in latched:
            # The evidenced 4.0s, not the 30s latch-confirm timeout --
            # this is measuring recovery, not justifying a latch.
            _PROBE_STATE.timeout = _REPROBE_TIMEOUT_SECS
            start = time.perf_counter()
            try:
                status, detail = record._probe()
            except Exception as exc:  # pragma: no cover - defensive, mirrors _run_one
                status, detail = ServiceStatus.OFFLINE, f"{type(exc).__name__}: {exc}"
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                _PROBE_STATE.timeout = None
                # Cleared here rather than after the try block so a probe
                # that raised does not leave the handle dangling. One
                # consequence, stated rather than discovered later: this
                # makes measurement once-per-service-per-run -- a second
                # call finds no probe and skips, which is what we want
                # from something whose whole contract is "observe once".
                record._probe = None

            latched_for = f"{time.monotonic() - record.latched_at:.0f}s" if record.latched_at else "unknown"
            if status == ServiceStatus.OFFLINE:
                logger.warning(
                    f"[latch-measurement] {record.name} was still offline at end of run "
                    f"({detail}) after {elapsed_ms:.0f} ms; latch stood {latched_for} and "
                    f"suppressed {record.skipped_count} retries. Latch was CORRECT.{_ctx()}"
                )
            else:
                # The case the whole feature exists for.
                logger.warning(
                    f"[latch-measurement] {record.name} was latched offline at startup but is "
                    f"reachable now ({status.value}, {elapsed_ms:.0f} ms). Latch stood {latched_for} "
                    f"and needlessly suppressed {record.skipped_count} retries -- a bounded "
                    f"re-probe would have recovered this run. Latch was FALSE.{_ctx()}"
                )

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

    def snapshot(self) -> List[Dict[str, Any]]:
        """
        Serialisable copy of what `print_summary()` prints, for the run
        document (`report/json_builder.py` -> `service_health`).

        Why this exists: every failure this registry counts has already
        been RETRIED AND HANDLED by the client that reported it, so it
        never reaches the per-variant `errors` list (which collects
        stage exceptions only, see `pipeline/orchestrator.py`). Until
        this method, that meant a run in which Ensembl failed three
        times and recovered wrote `errors: []`, `run_complete: true`
        and four output artefacts that said nothing about it, while the
        console printed `Ensembl ... Intermittent (2 failures)` and
        exited. Handled degradation is still degradation; a reader of
        the report could not see it, and the log is not part of the
        output bundle.

        This is the same fix, and the same reasoning, as the run-level
        `caveats` key in `report/json_builder.py::build` (printed by
        every renderer, absent from the document) and the `qc_metrics`
        field on that class ("a caveat in the document survives; an
        argument passed once does not").

        Deliberately reuses `_summary_status` -- the exact function the
        console summary uses -- so the document and the console cannot
        drift into saying different things about the same run.

        Pure read of already-collected counters: no probing, no network,
        no mutation. Safe to call at report-build time.
        """
        with self._lock:
            records = list(self._records.values())
        return [
            {
                "service": record.name,
                # Same string the console prints, from the same function.
                "status": self._summary_status(record),
                # See `ServiceRecord.is_local_component`: lets the
                # caveat renderer say "a component failed and its
                # result was withheld" rather than the data-source
                # sentence about retries, which would be false here.
                "is_local_component": record.is_local_component,
                # The one field a renderer needs to decide whether to
                # raise a flag, so no renderer has to re-derive
                # "degraded" from the counters and get it subtly wrong.
                "degraded": bool(record.failure_count) or record.startup_status == ServiceStatus.OFFLINE,
                "startup_status": record.startup_status.value,
                "failure_count": record.failure_count,
                "success_count": record.success_count,
                "skipped_count": record.skipped_count,
                "latency_ms": record.latency_ms,
            }
            for record in sorted(records, key=lambda r: r.name)
        ]

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
    # `CONFIG.indigenomes.ENABLED` now defaults to False (IndiGenomes
    # retired from GEPER's active query path 2026-08-08 -- a
    # commercial-use licensing restriction, see
    # `config.py::IndiGenomesConfig`'s docstring and
    # `DATA_SOURCE_LICENSE_AUDIT.md`), so this check is skipped by
    # default and no dangling "IndiGenomes" HEALTH entry appears for a
    # source no longer queried. Left gated on the same flag (not
    # deleted) rather than hardcoded off, so re-enabling the flag to
    # reinstate the integration also restores this probe automatically.
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
