"""Caching for InterPro lookups -- see `utils/simple_cache.py::SimpleTTLCache` for the shared implementation."""

from typing import Optional

from utils.simple_cache import SimpleTTLCache


class InterProCache(SimpleTTLCache):
    """Thread-safe, bounded, TTL'd LRU cache of InterPro lookup results (as plain dicts), keyed by UniProt accession."""

    def __init__(self, max_size: int = 5000, ttl_seconds: Optional[float] = 24 * 3600, disk_path: Optional[str] = None):
        super().__init__(max_size=max_size, ttl_seconds=ttl_seconds, disk_path=disk_path, label="InterPro")
