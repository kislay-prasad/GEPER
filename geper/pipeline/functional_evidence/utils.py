"""
Shared, dependency-free helpers for the PS3/BS3 functional-evidence
integration: ACMG/AMP evidence-code strength parsing and HGVS
accession-version-tolerant matching.

Genomic HGVS (`NC_000017.11:g.43106534C>A`) is stable enough that
GEPER's own `pipeline/hgvs_utils.py::to_hgvs_g` output can be compared
to a ClinGen ERepo record's `hgvs` list by exact string membership --
both sides pin the same RefSeq chromosome accession version for a
given assembly. Coding HGVS is not: MaveDB's transcript accession
version (e.g. `NM_007294.3`) frequently differs from whatever specific
RefSeq transcript version GEPER's own transcript-structure lookup
resolved (e.g. `NM_007294.4`) for the same gene, even though the
underlying c. numbering is identical between versions for the vast
majority of transcripts. `normalize_hgvs_c` strips the accession
version for exactly this comparison -- documented here rather than
silently assumed, since a genuine version-driven c. renumbering (rare,
but real for a transcript whose UTR annotation changed) would produce
a false match this comparison cannot detect.
"""

from __future__ import annotations

import re
from typing import Optional

# ACMG/AMP evidence-code suffix a VCEP specification can attach to
# override the default strength for that specific code (e.g. an
# evidence code literally labeled "PS3_Moderate" in a ClinGen ERepo
# guideline). Matches the same convention already seen on PP4_Strong
# in real ERepo data.
_STRENGTH_SUFFIXES = {
    "very_strong": "very_strong",
    "strong": "strong",
    "moderate": "moderate",
    "supporting": "supporting",
    "standalone": "stand_alone",
}


def parse_evidence_code_strength(label: str, default: str) -> str:
    """
    Parses an ERepo evidence-code label like 'PS3', 'PS3_Moderate', or
    'BS3_Supporting' into (bare code, strength). Returns `default` when
    the label carries no explicit strength suffix.
    """
    if "_" not in label:
        return default
    _, _, suffix = label.partition("_")
    return _STRENGTH_SUFFIXES.get(suffix.strip().lower(), default)


def bare_evidence_code(label: str) -> str:
    """'PS3_Moderate' -> 'PS3'; 'PS3' -> 'PS3'."""
    return label.split("_", 1)[0].strip().upper()


_ACCESSION_VERSION_RE = re.compile(r"^([A-Za-z]+_\d+)(?:\.\d+)?(:.*)$")


def normalize_hgvs_c(hgvs_c: Optional[str]) -> Optional[str]:
    """Strips the transcript accession's version number for
    version-tolerant comparison, e.g. 'NM_007294.4:c.181T>G' ->
    'NM_007294:c.181T>G'. Returns None unchanged (nothing to compare)."""
    if not hgvs_c:
        return None
    match = _ACCESSION_VERSION_RE.match(hgvs_c.strip())
    if not match:
        return hgvs_c.strip()
    return f"{match.group(1)}{match.group(2)}"
