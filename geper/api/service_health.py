"""
geper/api/service_health.py
───────────────────────────
What the three processing services (the interpretation API, the submission worker, the exception retry worker) share for
being run as supervised processes: a check of their configuration before they take any work, a heartbeat the supervisor
can read, and a stop signal that lets a worker finish what it is doing.

CONFIGURATION CHECK. `validate(role)` returns the problems found, as `(code, message)` pairs, and nothing else: no
secrets, no DSN, no key. The worker entry points refuse to start (exit 1) on any problem; the API refuses at startup and
reports the CODES ONLY on `/readyz`. Nothing here weakens a security check: the VCF root must be a real, readable,
non-world-writable directory (the storage policy of docs/VCF_STORAGE.md) and there is no switch that turns that off.

HEARTBEAT. A worker that has crashed is restarted by the supervisor; a worker that is alive but has stopped looping is
what a heartbeat catches. Each worker rewrites a small file (`GEPER_HEARTBEAT_FILE`, default under /tmp) on every pass of
its loop, and `python -m api.service_health heartbeat <role>` exits non-zero when that file is missing or older than
`--max-age` (default: the retry worker 300 s, the submission worker one full engine timeout plus 300 s, because it
does not loop while an engine run is in progress).

STOP. `install_stop_handlers(worker)` turns SIGTERM / SIGINT into `worker.request_stop()`: the worker finishes the job it
is on and does not start another. A job still running when the supervisor's grace period ends is killed; its row is left
`running`, is marked `interrupted` by the next start, and is completed only if its clinical record had already been
written (nothing partial is ever published: the record is written in one transaction after the run finishes).
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .vcf_paths import VcfPathError, vcf_root_from_env

ROLES = ("api", "submission_worker", "retry_worker")
HEARTBEAT_ENV = "GEPER_HEARTBEAT_FILE"
POLL_INTERVAL_ENV = "GEPER_WORKER_POLL_INTERVAL"

Problem = Tuple[str, str]  # (stable code, human message with no secret in it)


def _store_path() -> Path:
    configured = os.getenv("GEPER_SUBMISSION_STORE_PATH")
    return Path(configured) if configured else Path(__file__).parent / ".submissions.db"


def _check_vcf_root() -> List[Problem]:
    try:
        root = vcf_root_from_env()
    except VcfPathError as exc:
        return [("vcf_root_not_configured", f"GEPER_VCF_ROOT must name an existing directory ({exc.code}).")]
    problems: List[Problem] = []
    if not os.access(root, os.R_OK | os.X_OK):
        problems.append(("vcf_root_not_readable", "GEPER_VCF_ROOT is not readable by this process."))
    if os.name == "posix" and root.stat().st_mode & stat.S_IWOTH:
        problems.append(
            (
                "vcf_root_world_writable",
                "GEPER_VCF_ROOT is writable by everyone: anyone who can write there chooses what the engine reads. "
                "Only the platform may write under it.",
            )
        )
    return problems


def _check_store() -> List[Problem]:
    parent = _store_path().parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return [("store_dir_not_writable", "The directory for GEPER_SUBMISSION_STORE_PATH is missing or not writable.")]
    return []


def _check_clinical_dsn() -> List[Problem]:
    if not (os.getenv("CLINICAL_DSN") or "").strip():
        return [("clinical_dsn_missing", "CLINICAL_DSN is not set (the clinical database both workers write to).")]
    return []


def _check_engine_and_output() -> List[Problem]:
    from .submission_worker import _DEFAULT_GEPER_MAIN_PATH, _DEFAULT_OUTPUT_ROOT

    problems: List[Problem] = []
    engine = os.getenv("BIJ_AI_CLI_PATH", _DEFAULT_GEPER_MAIN_PATH)
    if not Path(engine).is_file():
        problems.append(("engine_not_found", "BIJ_AI_CLI_PATH (or the default geper/main.py) is not a file."))
    output_root = Path(os.getenv("GEPER_SUBMISSION_OUTPUT_ROOT", _DEFAULT_OUTPUT_ROOT))
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not output_root.is_dir() or not os.access(output_root, os.W_OK | os.X_OK):
        problems.append(("output_root_not_writable", "GEPER_SUBMISSION_OUTPUT_ROOT is missing or not writable."))
    return problems


def validate(role: str) -> List[Problem]:
    """Every configuration problem that would stop `role` from doing its job safely. Empty list = fit to start."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    problems: List[Problem] = []
    if role in ("api", "submission_worker"):
        problems += _check_vcf_root()
    problems += _check_store()
    if role in ("submission_worker", "retry_worker"):
        problems += _check_clinical_dsn()
    if role == "submission_worker":
        problems += _check_engine_and_output()
    return problems


def require_valid(role: str) -> None:
    """Worker entry points: print every problem and exit 1, before the process takes any work."""
    problems = validate(role)
    if problems:
        for code, message in problems:
            sys.stderr.write(f"ERROR: refusing to start ({role}) -- {code}: {message}\n")
        sys.exit(1)


# ── heartbeat ────────────────────────────────────────────────────────────────


def heartbeat_path(role: str) -> Path:
    return Path(os.getenv(HEARTBEAT_ENV) or f"/tmp/geper-{role}.heartbeat")  # noqa: S108 -- a per-container tmpfs file


def make_heartbeat(role: str) -> Callable[[], None]:
    """A callable that records 'alive now'. Failure to write is never allowed to stop the worker."""
    path = heartbeat_path(role)

    def beat() -> None:
        try:
            path.write_text(repr(time.time()), encoding="utf-8")
        except OSError:
            pass

    return beat


def default_max_age(role: str) -> float:
    if role == "submission_worker":
        from .submission_worker import INTERPRETATION_TIMEOUT_SECONDS

        return INTERPRETATION_TIMEOUT_SECONDS + 300.0
    return 300.0


def heartbeat_age(role: str) -> Optional[float]:
    """Seconds since the last beat, or None if there has never been one."""
    try:
        return max(0.0, time.time() - float(heartbeat_path(role).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def poll_interval(default: int) -> int:
    try:
        value = int(os.getenv(POLL_INTERVAL_ENV, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# ── stop signals ─────────────────────────────────────────────────────────────


def install_stop_handlers(worker) -> None:
    """SIGTERM and SIGINT ask the worker to stop after its current job (see the module docstring)."""

    def handle(signum, _frame) -> None:
        worker.request_stop()

    for name in (
        "SIGTERM",
        "SIGINT",
        "SIGBREAK",
    ):  # SIGBREAK: Windows' CTRL_BREAK, how a console process is asked to stop
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle)


# ── command line (used by the compose health checks) ─────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.service_health")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="print configuration problems for a role; exit 1 if any")
    check.add_argument("role", choices=ROLES)
    beat = sub.add_parser("heartbeat", help="exit 1 unless the role's heartbeat is recent")
    beat.add_argument("role", choices=ROLES)
    beat.add_argument("--max-age", type=float, default=None)
    args = parser.parse_args(argv)

    if args.command == "check-config":
        problems = validate(args.role)
        for code, message in problems:
            print(f"{code}: {message}")
        return 1 if problems else 0

    age = heartbeat_age(args.role)
    limit = args.max_age if args.max_age is not None else default_max_age(args.role)
    if age is None or age > limit:
        print(f"heartbeat for {args.role} is {'missing' if age is None else f'{age:.0f}s old'} (limit {limit:.0f}s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
