"""Caching for transcript-structure lookups -- see `utils/simple_cache.py::SimpleTTLCache` for the shared implementation."""

from typing import Optional

from utils.simple_cache import SimpleTTLCache


class TranscriptCache(SimpleTTLCache):
    """
    Thread-safe, bounded, TTL'd LRU cache of transcript-structure lookup
    results (as plain dicts), keyed by genome build + gene symbol.

    Exon coordinates for a released assembly do not change between
    Ensembl releases anywhere near as often as population frequencies
    do, so this defaults to the same long TTL as the ClinGen gene-level
    cache rather than gnomAD's short one.
    """

    def __init__(self, max_size: int = 5000, ttl_seconds: Optional[float] = 24 * 3600, disk_path: Optional[str] = None):
        super().__init__(max_size=max_size, ttl_seconds=ttl_seconds, disk_path=disk_path, label="Transcript")
