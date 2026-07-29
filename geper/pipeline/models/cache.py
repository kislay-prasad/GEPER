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
from typing import Optional

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


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
        """
        if expected_sha256 is None:
            return True
        if not path.is_file():
            return False
        actual = self.sha256_of(path)
        matches = actual.lower() == expected_sha256.lower()
        if not matches:
            logger.error(
                f"Checksum mismatch for cached weights at '{path}': "
                f"expected {expected_sha256}, got {actual}."
            )
        return matches

    def clear(self, plugin_key: str) -> None:
        directory = self.cache_dir / plugin_key
        if not directory.is_dir():
            return
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()
