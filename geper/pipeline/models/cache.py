"""
Generic on-disk weight cache for the plugin model framework.

Distinct from BOTH:
  - `utils.model_cache.ModelCache`: caches *loaded, in-memory model
    instances* for the HyenaDNA/Evo2/RNA-FM/ESM-2 family,
    process-lifetime only.
  - `pipeline.models.mmsplice.cache.MMSplicePredictionCache`: caches
    *prediction results* for MMSplice.

This one caches *downloaded weight files on disk*, keyed by plugin
name, so a plugin's (potentially multi-GB) weights are fetched once
and reused across process restarts -- the same role RNA-FM's
torch-hub checkpoint directory already plays for that model (see
`models/rna_fm.py`), generalized so every plugin gets it "for free"
by going through `ModelManager` instead of reimplementing its own
version of this per plugin.
"""

import hashlib
from pathlib import Path
from typing import NamedTuple, Optional

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


class ChecksumResult(NamedTuple):
    """What `verify_checksum` discards and `verify_checksum_detailed` keeps:
    the bytes-on-disk hash it already computed to reach `matches`, not a
    second read. `actual_sha256` is `None` exactly when no bytes were
    streamed to produce it (`expected_sha256 is None`, or the file is
    missing) -- never a placeholder for "not checked"."""

    matches: bool
    actual_sha256: Optional[str]


class WeightCache:
    """Bounded-nothing (weights are large and few; no eviction policy
    is needed the way `MMSplicePredictionCache`'s small result cache
    needs an LRU bound), on-disk, keyed by plugin name + filename."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir or CONFIG.splicing.PLUGIN_CACHE_DIR)

    def path_for(self, plugin_key: str, filename: str) -> Path:
        return self.cache_dir / plugin_key / filename

    def exists(self, plugin_key: str, filename: str) -> bool:
        return self.path_for(plugin_key, filename).is_file()

    def ensure_dir(self, plugin_key: str) -> Path:
        directory = self.cache_dir / plugin_key
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_checksum(self, path: Path, expected_sha256: Optional[str]) -> bool:
        """
        Returns True if `expected_sha256` is None (nothing to verify
        against -- e.g. no official checksum is published upstream,
        as is currently the case for RNA-FM's own weights) or if it
        matches; False on a genuine mismatch. Never raises for a
        missing file -- callers should check `exists()`/`is_file()`
        first if that distinction matters to them.

        A CALLER MUST DECIDE WHETHER "NOTHING TO VERIFY AGAINST" IS
        SAFE TO TREAT AS PASSING -- this function cannot make that
        judgment for you, and defaulting to True here means a caller
        that forgets to check is silently unprotected, not silently
        safe. `pipeline.provenance.verify_model_artifact` is today's
        only production caller; it never reaches this function with
        `expected_sha256=None` at all (see its own guard at
        `cache_declared_sha256 is None`, `pipeline/provenance.py:334`
        as of this writing), so a genuinely unrecorded hash there
        surfaces as `HashVerification.UNVERIFIABLE`, never as a
        default pass through here. A new caller must reproduce that
        guard itself -- nothing in this file enforces it.
        """
        return self.verify_checksum_detailed(path, expected_sha256).matches

    def verify_checksum_detailed(self, path: Path, expected_sha256: Optional[str]) -> "ChecksumResult":
        """
        Same rules as `verify_checksum` (see its docstring for the
        `expected_sha256 is None` / missing-file caveats), but also returns
        the bytes-on-disk hash this call computed to reach its verdict --
        the value `verify_checksum` has always thrown away after logging it.
        Added so `pipeline.provenance.verify_model_artifact` can persist the
        observed hash into the run's own record instead of leaving it only
        in this log line; `verify_checksum` itself is UNCHANGED, a thin
        wrapper over this, kept for its five existing test assertions and
        any other bool-only caller.
        """
        if expected_sha256 is None:
            return ChecksumResult(matches=True, actual_sha256=None)
        if not path.is_file():
            return ChecksumResult(matches=False, actual_sha256=None)
        actual = self.sha256_of(path)
        matches = actual.lower() == expected_sha256.lower()
        if not matches:
            logger.error(f"Checksum mismatch for cached weights at '{path}': expected {expected_sha256}, got {actual}.")
        return ChecksumResult(matches=matches, actual_sha256=actual)

    def clear(self, plugin_key: str) -> None:
        directory = self.cache_dir / plugin_key
        if not directory.is_dir():
            return
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()
