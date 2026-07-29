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

    def affected_domains(self, position: Optional[int]) -> List[Dict[str, Any]]:
        """
        Domains whose matched span overlaps `position` (a best-effort
        protein residue index -- see the orchestrator's
        `_estimate_protein_position` docstring for exactly what this
        position does and does not guarantee). Returns an empty list
        (never None) when `position` is None, matching this codebase's
        convention of an empty/absent result over a fabricated one.
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
        }

    @staticmethod
    def not_found(accession: str, source: str) -> "InterProAnnotation":
        return InterProAnnotation(accession=accession, source=source, found=False)

    @staticmethod
    def from_error(accession: str, error: str) -> "InterProAnnotation":
        return InterProAnnotation(accession=accession, source="error", found=False, error=error)
