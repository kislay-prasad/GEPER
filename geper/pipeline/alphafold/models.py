"""
Data model for AlphaFold DB structural evidence.

Kept separate from `pipeline.uniprot.models` / `pipeline.interpro.models`
-- this module models a structural reference (predicted model URL,
version, confidence) for a protein (keyed by UniProt accession), not
its function/domain annotation. Plain, JSON-serializable dataclasses,
no network/IO dependency (the actual structure-file download/parsing
lives in `pipeline/alphafold/provider.py`).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def confidence_band(plddt: Optional[float], *, very_high: float, confident: float, low: float) -> Optional[str]:
    """
    Map a pLDDT score to AlphaFold DB's own published confidence bands
    (https://alphafold.ebi.ac.uk/faq): >90 very high, 70-90 confident,
    50-70 low, <50 very low. Thresholds are configurable
    (`CONFIG.alphafold.PLDDT_*_THRESHOLD`) but default to AlphaFold's
    exact published cutoffs.
    """
    if plddt is None:
        return None
    if plddt >= very_high:
        return "very_high"
    if plddt >= confident:
        return "confident"
    if plddt >= low:
        return "low"
    return "very_low"


@dataclass
class AlphaFoldAnnotation:
    """Complete AlphaFold DB evidence record for one protein (by UniProt accession)."""

    accession: str
    source: str  # "local_dataset" | "alphafold_db_api" | "cache" | "error"
    found: bool = False

    model_version: Optional[str] = None
    pdb_url: Optional[str] = None
    cif_url: Optional[str] = None
    uniprot_start: Optional[int] = None
    uniprot_end: Optional[int] = None

    mean_plddt: Optional[float] = None
    mean_plddt_band: Optional[str] = None

    # Only populated when a residue position estimate is available AND
    # `CONFIG.alphafold.FETCH_STRUCTURE_FILE` is on (see
    # `provider.py::LiveAPIAlphaFoldProvider`) -- the full per-residue
    # pLDDT array is never surfaced in the per-variant result (it can
    # be thousands of entries long for a large protein); only the
    # value at the estimated affected residue is.
    affected_residue_plddt: Optional[float] = None
    affected_residue_band: Optional[str] = None
    protein_position_basis: Optional[str] = None

    structure_fetched: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accession": self.accession,
            "source": self.source,
            "found": self.found,
            "model_version": self.model_version,
            "pdb_url": self.pdb_url,
            "cif_url": self.cif_url,
            "uniprot_start": self.uniprot_start,
            "uniprot_end": self.uniprot_end,
            "mean_plddt": self.mean_plddt,
            "mean_plddt_band": self.mean_plddt_band,
            "affected_residue_plddt": self.affected_residue_plddt,
            "affected_residue_band": self.affected_residue_band,
            "protein_position_basis": self.protein_position_basis,
            "structure_fetched": self.structure_fetched,
            "error": self.error,
        }

    @staticmethod
    def not_found(accession: str, source: str) -> "AlphaFoldAnnotation":
        return AlphaFoldAnnotation(accession=accession, source=source, found=False)

    @staticmethod
    def from_error(accession: str, error: str) -> "AlphaFoldAnnotation":
        return AlphaFoldAnnotation(accession=accession, source="error", found=False, error=error)
