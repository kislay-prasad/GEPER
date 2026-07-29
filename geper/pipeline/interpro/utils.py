"""
Shared, dependency-free helpers for the InterPro/Pfam integration:
UniProt-accession normalization/cache keys, and parsing InterPro's
public REST API JSON response into `InterProAnnotation`/
`InterProDomainMatch`. No network, no torch -- pure functions.
"""

from typing import Any, Dict, List, Optional

from pipeline.interpro.models import InterProAnnotation, InterProDomainMatch


def normalize_accession(accession: Optional[str]) -> Optional[str]:
    """UniProt accessions are case-sensitive as published, but InterPro's API accepts/returns them upper-cased."""
    if not accession:
        return None
    return accession.strip().upper() or None


def accession_cache_key(accession: str) -> str:
    return f"accession:{normalize_accession(accession)}"


# ---------------------------------------------------------------------------
# Parsing InterPro's `/entry/all/protein/uniprot/{accession}/` response
# ---------------------------------------------------------------------------
#
# Response shape (https://www.ebi.ac.uk/interpro/api/, "entry" endpoint
# group, filtered to `protein/uniprot/{accession}`):
#   {"results": [{"metadata": {...}, "proteins": [{"entry_protein_locations": [...]}]}]}
# `source_database` in metadata is "interpro" for an integrated
# InterPro entry, or the member database's own name ("pfam", "smart",
# "prosite_profiles", "cdd", ...) for a not-yet-integrated (or
# member-DB-specific) signature match -- this integration surfaces
# both, since Pfam calls are explicitly required by GEPER's spec and
# not every Pfam family is (yet) integrated into a distinct InterPro
# entry.


def _first_fragment_span(locations: List[Dict[str, Any]]) -> "tuple[Optional[int], Optional[int]]":
    """Best (widest) start/end span across every location's fragments, for one protein match."""
    start: Optional[int] = None
    end: Optional[int] = None
    for location in locations:
        for fragment in location.get("fragments") or []:
            f_start, f_end = fragment.get("start"), fragment.get("end")
            if f_start is None or f_end is None:
                continue
            start = f_start if start is None else min(start, f_start)
            end = f_end if end is None else max(end, f_end)
    return start, end


def parse_interpro_response(accession: str, payload: Dict[str, Any], source: str) -> InterProAnnotation:
    """Parse an InterPro `/entry/.../protein/uniprot/{accession}/` JSON response into an `InterProAnnotation`."""
    results = payload.get("results") or []
    domains: List[InterProDomainMatch] = []

    for entry in results:
        metadata = entry.get("metadata") or {}
        source_database = (metadata.get("source_database") or "").lower() or None
        entry_accession = metadata.get("accession")

        proteins = entry.get("proteins") or []
        start = end = None
        for protein in proteins:
            locations = protein.get("entry_protein_locations") or protein.get("protein_locations") or []
            start, end = _first_fragment_span(locations)
            if start is not None:
                break

        is_interpro_integrated = source_database == "interpro"
        domains.append(
            InterProDomainMatch(
                interpro_accession=entry_accession if is_interpro_integrated else metadata.get("integrated"),
                name=metadata.get("name"),
                short_name=metadata.get("short_name") or metadata.get("name"),
                entry_type=metadata.get("type"),
                member_database=source_database,
                member_accession=None if is_interpro_integrated else entry_accession,
                start=start,
                end=end,
            )
        )

    return InterProAnnotation(accession=accession, source=source, found=bool(domains), domains=domains)
