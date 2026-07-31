"""
Data model for UniProt evidence.

Kept deliberately separate from `pipeline/vcf_parser.py::Variant` and
from every other model result shape (AlphaMissense, MMSplice, gnomAD,
ClinGen) -- this module models the *evidence UniProt returns for a
gene's canonical reviewed protein*, not the variant itself. All
dataclasses are plain, JSON-serializable (via `to_dict()`), and have no
network/IO dependency whatsoever.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UniProtFeature:
    """One UniProtKB sequence feature (domain, region, active site, binding site, modified residue, etc.)."""

    feature_type: str
    description: Optional[str] = None
    begin: Optional[int] = None
    end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_type": self.feature_type,
            "description": self.description,
            "begin": self.begin,
            "end": self.end,
        }

    def overlaps(self, position: Optional[int]) -> bool:
        """Whether a (best-effort) protein residue position falls within this feature's span."""
        if position is None or self.begin is None or self.end is None:
            return False
        return self.begin <= position <= self.end


@dataclass
class UniProtAnnotation:
    """Complete UniProt evidence record for one gene's canonical reviewed protein entry."""

    gene_symbol: str
    source: str  # "local_dataset" | "uniprot_rest_api" | "cache" | "error"
    found: bool = False

    accession: Optional[str] = None
    entry_name: Optional[str] = None
    protein_name: Optional[str] = None
    organism: Optional[str] = None
    reviewed: bool = False
    sequence_length: Optional[int] = None

    function_text: Optional[str] = None
    disease_comments: List[str] = field(default_factory=list)
    features: List[UniProtFeature] = field(default_factory=list)

    error: Optional[str] = None

    # UniProt's own real, source-published release identifiers, from
    # the live REST API's `X-UniProt-Release`/`X-UniProt-Release-Date`
    # response headers (verified live, e.g. release "2026_02"). None
    # for the local-dataset provider or a call that never reached the
    # live API. See `pipeline/provenance.py`'s docstring for the audit.
    release: Optional[str] = None
    release_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gene_symbol": self.gene_symbol,
            "source": self.source,
            "found": self.found,
            "accession": self.accession,
            "entry_name": self.entry_name,
            "protein_name": self.protein_name,
            "organism": self.organism,
            "reviewed": self.reviewed,
            "sequence_length": self.sequence_length,
            "function": self.function_text,
            "disease_comments": self.disease_comments,
            "features": [f.to_dict() for f in self.features],
            "error": self.error,
            "release": self.release,
            "release_date": self.release_date,
        }

    @staticmethod
    def not_found(gene_symbol: str, source: str) -> "UniProtAnnotation":
        return UniProtAnnotation(gene_symbol=gene_symbol, source=source, found=False)

    @staticmethod
    def from_error(gene_symbol: str, error: str) -> "UniProtAnnotation":
        return UniProtAnnotation(gene_symbol=gene_symbol, source="error", found=False, error=error)
