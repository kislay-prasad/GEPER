"""Caching for ClinVar codon-neighborhood lookups -- see `utils/simple_cache.py::SimpleTTLCache` for the shared implementation."""

from typing import Optional

from utils.simple_cache import SimpleTTLCache


class ClinVarCodonCache(SimpleTTLCache):
    """
    Thread-safe, bounded, TTL'd LRU cache of ClinVar codon-neighborhood
    lookup results (as plain dicts), keyed by transcript + codon
    number. A VCF with several variants clustered in the same hotspot
    codon (not unusual -- see e.g. TP53 codon 175/248/273) pays the
    ClinVar E-utilities round trip once per distinct codon, not once
    per variant.
    """

    def __init__(self, max_size: int = 5000, ttl_seconds: Optional[float] = 6 * 3600, disk_path: Optional[str] = None):
        super().__init__(max_size=max_size, ttl_seconds=ttl_seconds, disk_path=disk_path, label="ClinVarCodon")
