"""
Shared automatic-installation helper for optional PyPI dependencies.

Several GEPER dependencies (RNA-FM's `rna-fm`, BLAST's
`biopython`) are listed in requirements.txt but may not actually be
installed in a given Colab/runtime session -- e.g. `pip install -r
requirements.txt` was never run, or a runtime restart dropped a
previous session's installs. Rather than requiring the user to notice
which package is missing and install it by hand, GEPER auto-installs
any of these the first time they're actually needed, exactly once per
process, using the pattern already established for HyenaDNA's source
checkout (see models/hyenadna.py -- that one's a `git clone` rather
than a `pip install`, so it isn't routed through here, but the
"cheap check, install-once, cache the outcome" shape is identical).

Also provides the same shape for small, common *system* CLI tools
(e.g. htslib's `tabix`, used by models/alphamissense.py) via `apt-get`
-- see `ensure_system_binary_available` below.
"""

import enum
import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# Passed to every auto-install as `pip install --constraint <this file>`
# (see `ensure_pip_package_available` below). requirements.txt already
# carries every version pin GEPER's own core stack actually depends on
# (torch==2.7.1, transformers==4.56.2, torchvision==0.22.1, ...),
# so pointing pip's own constraint-resolution at it -- rather than
# hand-maintaining a second, parallel list of "packages that must not
# move" -- makes pip itself refuse (clean non-zero exit, no partial
# install) any optional auto-installed package whose own metadata
# would otherwise force one of those pins to move. Confirmed live
# 2026-08-20: without this, `pip install enformer-pytorch` (which pins
# `transformers==4.56.2` exactly) silently downgraded an
# already-installed `transformers==5.15.1` mid-process, corrupting
# HyenaDNA/ESM2 loading later in the same run via transformers' lazy
# submodule imports resolving against the now-downgraded on-disk tree.
_REQUIREMENTS_CONSTRAINT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt"
)

# Per-package cache of the auto-install outcome, so a package that
# genuinely can't be installed in this environment (no network, pip
# broken, etc) fails fast on every subsequent call instead of
# re-attempting a doomed `pip install` every time it's needed (e.g.
# once per variant that hits the stage requiring it).
# INVARIANT: this cache never holds a NOT_CHECKED outcome. It memoizes
# only DETERMINED results -- a package that was really checked and
# really found present or absent. A check that was deliberately skipped
# (the pytest branch below) is not a result and is recomputed fresh on
# every call; see `check_pip_package_availability`. Caching "I did not
# look" as if it were "I looked and it was missing" is the exact
# conflation PackageCheckStatus exists to prevent, and it would also
# poison any later test that forces the real path for the same package.
_INSTALL_RESULTS: Dict[str, bool] = {}

# Same idea, separate namespace, for apt-installed system binaries
# (see ensure_system_binary_available).
_SYSTEM_BINARY_INSTALL_RESULTS: Dict[str, bool] = {}


class PackageCheckStatus(str, enum.Enum):
    """
    `check_pip_package_availability`'s return, per
    CI-geper-suite-live-installs-unpinned-packages-mid-run-results-depend-on-order
    / RULED-codebase-gets-an-explicit-not-checked-state-distinct-from-checked-and-absent.

    NOT_CHECKED and ABSENT are deliberately two different states, not
    one -- the same reason `StageStatus`, `VersionStatus` and
    `ClinVarMatchStatus` each keep "didn't check" distinct from
    "checked, found nothing". Conflating them is the bug this type
    exists to prevent, and conflating a flag-plus-optional-string
    version of the same distinction already caused a real one in this
    codebase (`pipeline/gnomad/models.py`'s `error` field, commit
    9e2a3ee: ten consumers read `error` for truthiness instead of
    presence, so an exception with an empty message passed every guard
    as a confirmed absence). Checked here structurally rather than by
    convention.

    JSON-SAFE BY CONSTRUCTION, NOT BY CONVENTION -- READ BEFORE
    CHANGING THE BASE CLASSES. `json.dumps(PackageCheckStatus.X)` emits
    the member's plain string value, with no caller ever needing to
    write `.value`: json's encoder special-cases `isinstance(o, str)`
    before it consults any custom encoder, `__str__`, or `__bool__`,
    and every member genuinely IS a `str` instance because of the `str`
    mixin below. That also means the `__bool__` raise below and JSON
    safety travel on different protocols and can never conflict. This
    is a DIRECT, FRAGILE consequence of that one base class: changing
    it -- to a plain `enum.Enum`, an `IntEnum`, a dataclass, anything
    that drops the `str` base -- silently breaks every provenance or
    report export that serializes this value with no special handling,
    and nothing announces the break until a live `json.dumps()` raises
    `TypeError: Object of type PackageCheckStatus is not JSON
    serializable` in production rather than in review. Re-audit every
    serialization boundary this value crosses before changing them.
    """

    NOT_CHECKED = "not_checked"  # auto-install intentionally skipped (e.g. under pytest); real availability unknown
    ABSENT = "absent"  # genuinely checked: not importable, and a real install attempt failed
    PRESENT = "present"  # importable, whether pre-existing or freshly installed

    def __bool__(self):
        raise TypeError(
            "PackageCheckStatus has no truth value -- `if x:`/`if not x:` would "
            "silently misread NOT_CHECKED as either PRESENT or ABSENT depending on "
            "encoding, which is exactly the bug this type exists to prevent. "
            "Compare explicitly, e.g. `x == PackageCheckStatus.PRESENT`."
        )


def is_pip_package_installed(import_name: str) -> bool:
    """Cheap, import-free check for whether `import_name` is importable."""
    return importlib.util.find_spec(import_name) is not None


def installed_package_version(dist_name: str) -> Optional[str]:
    """
    The version of the INSTALLED distribution `dist_name`, or `None`
    when it is not installed / has no readable metadata.

    Import-free, the same way `is_pip_package_installed` above is:
    `importlib.metadata` reads the distribution's metadata off disk
    and never imports the package itself. That matters for the one
    caller this was added for -- GEPER deliberately never imports
    `mmsplice`'s own Python code (it uses only the bundled weight
    files; see `pipeline/models/mmsplice/loader.py`), so asking it for
    `__version__` would mean importing something the pipeline is
    designed not to import.

    `None` is a real answer and callers must record it as one. The
    package this exists for is auto-installed lazily on first use, so
    "not installed" is the correct reading at any point before a
    variant has needed it -- and a provenance record that substituted
    a configured or expected version there would be reasserting the
    exact false claim this function was added to remove.
    """
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 -- provenance must never break a run
        logger.debug(f"Could not read installed version for '{dist_name}': {exc}")
        return None


def _auto_install_disabled_for_tests() -> bool:
    """
    True when this process is running under pytest -- in which case
    `ensure_pip_package_available` must not spawn a real `pip install`
    subprocess. See CI-geper-suite-live-installs-unpinned-packages-mid-
    run-results-depend-on-order: a live, unpinned install running mid-
    test-run made package availability mutable process state, so test
    outcomes depended on execution order and network reachability
    (proven live in CI run 32599323393 -- `enformer_pytorch` went
    ABSENT -> PRESENT inside a single pytest process). Reporting the
    package unavailable instead is also the honest answer: in CI those
    optional packages genuinely are not installed.

    `"pytest" in sys.modules` rather than `PYTEST_CURRENT_TEST` (only
    set while a specific test's `call` phase is executing) so this
    covers collection and fixture setup too, not just in-test calls.

    Extracted as its own function, rather than inlining the check,
    purely so this module's OWN tests of the real subprocess-invocation
    mechanics (constraint flag, conflict handling) can patch this one
    seam to force the real path deliberately -- see test_auto_install.py.
    Runtime behaviour outside pytest (the auto-install pattern itself,
    documented in requirements.txt lines 5-13) is completely unchanged.
    """
    return "pytest" in sys.modules


def check_pip_package_availability(pip_name: str, import_name: Optional[str] = None) -> PackageCheckStatus:
    """
    Idempotent, automatic setup: if `import_name` (defaults to
    `pip_name`) isn't already importable, installs it via
    `pip install <pip_name>` -- exactly once per process -- so the
    pipeline can proceed without a manual setup step.

    Handles PEP 668 "externally-managed-environment" refusals (common
    on Debian/Ubuntu system Python images, some Colab runtimes) by
    retrying once with --break-system-packages: this process is a
    disposable pipeline run, not a shared system install a user is
    curating by hand.

    Constrained by `--constraint requirements.txt` (see
    `_REQUIREMENTS_CONSTRAINT_PATH` above) so this can never silently
    downgrade a core, already-pinned dependency (e.g. transformers) to
    satisfy an optional package's own conflicting metadata pin -- pip
    refuses the install outright instead, which the existing
    non-zero-exit handling below already reports as a normal failure.

    Returns a `PackageCheckStatus`, NOT a bool: PRESENT if importable
    after the attempt, ABSENT if a real attempt was made and it is
    still not importable, and NOT_CHECKED if no attempt was made at all
    because auto-install is disabled under pytest. The third state is
    the point -- a caller that reports "could not be installed" for a
    check that never ran is stating something false, which is what this
    function's bool predecessor made unavoidable.

    Callers indifferent to WHY should keep using
    `ensure_pip_package_available` below.
    """
    import_name = import_name or pip_name

    if is_pip_package_installed(import_name):
        return PackageCheckStatus.PRESENT

    # Only ever holds a DETERMINED outcome -- see _INSTALL_RESULTS above.
    if pip_name in _INSTALL_RESULTS:
        return PackageCheckStatus.PRESENT if _INSTALL_RESULTS[pip_name] else PackageCheckStatus.ABSENT

    if _auto_install_disabled_for_tests():
        logger.info(
            f"'{import_name}' is not importable and auto-install is "
            "disabled while running under pytest -- reporting it "
            "unavailable rather than installing it from PyPI mid-test-"
            "run (see CI-geper-suite-live-installs-unpinned-packages-"
            "mid-run-results-depend-on-order). Real, non-test runs are "
            "unaffected."
        )
        # Deliberately NOT written to _INSTALL_RESULTS: a skipped check
        # is not a result. The memo exists only to avoid re-running the
        # subprocess, and this branch never reaches the subprocess, so
        # caching buys nothing here -- while a cached "False" would be
        # read back as a confirmed ABSENT by the lookup above.
        return PackageCheckStatus.NOT_CHECKED

    logger.info(
        f"'{import_name}' is not yet installed; installing it "
        f"automatically now (pip install {pip_name}) so the pipeline "
        "can proceed without a manual setup step."
    )
    base_cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--constraint",
        _REQUIREMENTS_CONSTRAINT_PATH,
        pip_name,
    ]
    result = subprocess.run(base_cmd, capture_output=True, text=True)
    if result.returncode != 0 and "externally-managed-environment" in (result.stderr or ""):
        logger.info("pip reported an externally-managed environment; retrying with --break-system-packages.")
        result = subprocess.run(base_cmd + ["--break-system-packages"], capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(
            f"Automatic '{pip_name}' installation failed (exit "
            f"{result.returncode}): {(result.stderr or '').strip()[-500:]}"
        )
        _INSTALL_RESULTS[pip_name] = False
        return PackageCheckStatus.ABSENT

    importlib.invalidate_caches()
    ok = is_pip_package_installed(import_name)
    _INSTALL_RESULTS[pip_name] = ok
    if not ok:
        logger.error(
            f"'pip install {pip_name}' completed but '{import_name}' still "
            "isn't importable; check the install output above."
        )
    # `ok` is a plain bool from is_pip_package_installed, deliberately
    # not a PackageCheckStatus -- `if not ok` above would raise if it
    # were one.
    return PackageCheckStatus.PRESENT if ok else PackageCheckStatus.ABSENT


def ensure_pip_package_available(pip_name: str, import_name: Optional[str] = None) -> bool:
    """
    Back-compat bool surface for callers that only ever want "can I use
    it or not" and are indifferent to WHY -- returns True iff PRESENT,
    and False for both NOT_CHECKED and ABSENT.

    Byte-identical in behaviour to this function's pre-tri-state
    contract, because the callers that still use it never distinguished
    the two False reasons to begin with. If you are about to write a
    reason string, a log line or an exception message on the False
    branch, this is the WRONG function: call
    `check_pip_package_availability` and branch on the three states,
    or you will tell the user a package "could not be installed" when
    nothing was ever attempted.
    """
    return check_pip_package_availability(pip_name, import_name) == PackageCheckStatus.PRESENT


def is_system_binary_available(binary_name: str) -> bool:
    """Cheap, subprocess-free check for whether `binary_name` is on PATH."""
    return shutil.which(binary_name) is not None


def ensure_system_binary_available(binary_name: str, apt_package: Optional[str] = None) -> bool:
    """
    Idempotent, automatic setup for a small, common system CLI tool
    (e.g. htslib's `tabix`) via `apt-get install`, using the identical
    "cheap check, install-once, cache the outcome" shape as
    `ensure_pip_package_available` above and
    `models/hyenadna.py::ensure_hyenadna_available` -- so an optional
    system dependency that happens to be missing from a fresh Colab
    runtime is auto-installed the same way a missing pip package or
    HyenaDNA's git checkout already is, instead of silently degrading
    the whole stage to "unavailable" just because nobody ran a manual
    `!apt-get install` cell first.

    Only attempts `apt-get` (Debian/Ubuntu, which every stock Colab
    runtime is) -- there's no single cross-platform system package
    manager to target automatically, so on any other OS (or if
    `apt-get` itself isn't present) this returns False without
    attempting anything further, and the caller degrades gracefully
    exactly like a plain "not installed" outcome always has.
    """
    apt_package = apt_package or binary_name

    if is_system_binary_available(binary_name):
        return True

    if binary_name in _SYSTEM_BINARY_INSTALL_RESULTS:
        return _SYSTEM_BINARY_INSTALL_RESULTS[binary_name]

    if shutil.which("apt-get") is None:
        logger.info(
            f"'{binary_name}' is not installed and 'apt-get' is not "
            "available on this system to install it automatically "
            f"(not a Debian/Ubuntu environment). Install '{apt_package}' "
            "manually (or via your platform's package manager) if you "
            "want this stage to run."
        )
        _SYSTEM_BINARY_INSTALL_RESULTS[binary_name] = False
        return False

    logger.info(
        f"'{binary_name}' is not yet installed; installing it "
        f"automatically now (apt-get install -y {apt_package}) so the "
        "pipeline can proceed without a manual setup step."
    )

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    # `apt-get update` failing (e.g. a stale/partial index, a flaky
    # mirror) shouldn't by itself block the install attempt -- the
    # existing local index is often still good enough for a small,
    # long-stable package like tabix, so we log and proceed rather
    # than giving up early.
    update_result = _run(["apt-get", "update", "-qq"])
    if update_result.returncode != 0:
        logger.warning(
            "'apt-get update' failed (exit "
            f"{update_result.returncode}); proceeding to try the "
            "install anyway in case the local package index is "
            f"already sufficient: {(update_result.stderr or '').strip()[-500:]}"
        )

    install_result = _run(["apt-get", "install", "-y", "-qq", apt_package])
    if install_result.returncode != 0:
        logger.error(
            f"Automatic '{apt_package}' installation via apt-get failed "
            f"(exit {install_result.returncode}): "
            f"{(install_result.stderr or '').strip()[-500:]}"
        )
        _SYSTEM_BINARY_INSTALL_RESULTS[binary_name] = False
        return False

    ok = is_system_binary_available(binary_name)
    _SYSTEM_BINARY_INSTALL_RESULTS[binary_name] = ok
    if not ok:
        logger.error(
            f"'apt-get install {apt_package}' completed but "
            f"'{binary_name}' still isn't on PATH; check the install "
            "output above."
        )
    return ok
