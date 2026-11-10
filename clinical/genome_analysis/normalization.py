"""
clinical/genome_analysis/normalization.py
──────────────────────────────────────
ONE canonical contig-name normaliser for the Genome Analysis module.

'chr1' / '1' / 'CHR1' -> '1';  'chrX' / 'x' -> 'X';  'chrM' / 'M' / 'MT' /
'chrMT' -> 'MT'.

Anything that is not one of chromosomes 1-22, X, Y or the mitochondrion returns
None -- unplaced scaffolds ('GL000220.1'), alt loci, decoys ('chrUn_...',
'hs37d5'), 'chr23', 'chr0', 'chr01', empty strings, non-strings. The caller
decides what None means (the aggregator counts it as UNMAPPED and says so; it
never drops it silently and never guesses).

This does not replace the per-resource normalisers elsewhere in the repository
(gnomad, ensembl, conservation, alphamissense): those have different output
conventions on purpose and are untouched.
"""

from __future__ import annotations

from typing import Optional

from .reference import CANONICAL_CHROMOSOMES

_CANONICAL = frozenset(CANONICAL_CHROMOSOMES)
_MITO = frozenset({"M", "MT"})


def normalize_contig(name: object) -> Optional[str]:
    if not isinstance(name, str):
        return None
    token = name.strip()
    if token[:3].lower() == "chr":
        token = token[3:]
    token = token.upper()
    if token in _MITO:
        return "MT"
    # Exact match only: '01', '+1', ' 1', '1.0' and '23' are not chromosomes here.
    if token in _CANONICAL:
        return token
    return None
