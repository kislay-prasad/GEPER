"""
Data model for InterPro/Pfam evidence.

Kept separate from `pipeline.uniprot.models` -- this module models
conserved-domain/family/site matches for a protein (keyed by UniProt
accession), not the protein's own function/disease annotation. Plain,
JSON-serializable dataclasses, no network/IO dependency.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InterProDomainMatch:
    """One conserved domain/family/site match against a protein sequence."""

    interpro_accession: Optional[str] = None
    name: Optional[str] = None
    short_name: Optional[str] = None
    entry_type: Optional[str] = None  # "domain" | "family" | "repeat" | "site" | ...
    member_database: Optional[str] = None  # e.g. "pfam", "smart", "prosite_profiles"
    member_accession: Optional[str] = None  # e.g. "PF00001"
    start: Optional[int] = None
    end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interpro_accession": self.interpro_accession,
            "name": self.name,
            "short_name": self.short_name,
            "type": self.entry_type,
            "member_database": self.member_database,
            "member_accession": self.member_accession,
            "start": self.start,
            "end": self.end,
        }

    def overlaps(self, position: Optional[int]) -> bool:
        """Whether a (best-effort) protein residue position falls within this domain's matched span."""
        if position is None or self.start is None or self.end is None:
            return False
        return self.start <= position <= self.end


@dataclass
class InterProAnnotation:
    """Complete InterPro/Pfam evidence record for one protein (by UniProt accession)."""

    accession: str  # UniProt accession queried
    source: str  # "local_dataset" | "interpro_rest_api" | "cache" | "error"
    found: bool = False

    domains: List[InterProDomainMatch] = field(default_factory=list)
    error: Optional[str] = None

    # The live REST API's own `InterPro-Version` response header
    # (verified live, e.g. "109.0") -- InterPro's real, source-
    # published version identifier for this data. None for the local-
    # dataset provider (no equivalent signal in a static file) or when
    # this annotation didn't come from a live API call at all (error/
    # not-found sentinels built without ever reaching `_get`). See
    # `pipeline/provenance.py`'s docstring for the full per-source audit.
    api_version: Optional[str] = None

    def affected_domains(self, position: Optional[int]) -> List[Dict[str, Any]]:
        """
        Domains whose matched span overlaps `position` (a
        transcript-verified canonical protein residue index -- see
        `pipeline/pvs1/utils.py::canonical_protein_position`'s
        docstring for exactly what this position does and does not
        guarantee). Returns an empty list (never None) when `position`
        is None.

        NOTE: unlike `InterProLookup.query_variant`'s own
        `affected_domains` field (see that module's docstring), this
        method is not on the code path the orchestrator actually
        calls -- `query_variant` filters `_NON_DOMAIN_ENTRY_TYPES`
        inline and returns `None`, not `[]`, when `position` is None,
        specifically so a caller can distinguish "not checked" from
        "checked, no overlap" (see the PM1 false-negative fix,
        2026-07-31). This method predates that distinction and
        currently has no caller; if it gains one, it should adopt the
        same three-valued convention rather than reintroducing the
        collapse.
        """
        if position is None:
            return []
        return [d.to_dict() for d in self.domains if d.overlaps(position)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accession": self.accession,
            "source": self.source,
            "found": self.found,
            "domains": [d.to_dict() for d in self.domains],
            "error": self.error,
            "api_version": self.api_version,
        }

    @staticmethod
    def not_found(accession: str, source: str) -> "InterProAnnotation":
        return InterProAnnotation(accession=accession, source=source, found=False)

    @staticmethod
    def from_error(accession: str, error: str) -> "InterProAnnotation":
        return InterProAnnotation(accession=accession, source="error", found=False, error=error)
