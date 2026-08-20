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

import importlib
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
# (torch==2.7.1, transformers>=5.12.1,<6.0.0, torchvision==0.22.1, ...),
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
_INSTALL_RESULTS: Dict[str, bool] = {}

# Same idea, separate namespace, for apt-installed system binaries
# (see ensure_system_binary_available).
_SYSTEM_BINARY_INSTALL_RESULTS: Dict[str, bool] = {}


def is_pip_package_installed(import_name: str) -> bool:
    """Cheap, import-free check for whether `import_name` is importable."""
    return importlib.util.find_spec(import_name) is not None


def ensure_pip_package_available(pip_name: str, import_name: Optional[str] = None) -> bool:
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

    Returns whether `import_name` is importable after the attempt.
    """
    import_name = import_name or pip_name

    if is_pip_package_installed(import_name):
        return True

    if pip_name in _INSTALL_RESULTS:
        return _INSTALL_RESULTS[pip_name]

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
        return False

    importlib.invalidate_caches()
    ok = is_pip_package_installed(import_name)
    _INSTALL_RESULTS[pip_name] = ok
    if not ok:
        logger.error(
            f"'pip install {pip_name}' completed but '{import_name}' still "
            "isn't importable; check the install output above."
        )
    return ok


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
