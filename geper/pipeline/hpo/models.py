"""
Data model for HPO (Human Phenotype Ontology) gene-phenotype evidence.

Same shape convention as `pipeline/clingen/models.py`: HPO gene-phenotype
annotation is gene-level (a gene is associated with a set of phenotype
terms, via one or more diseases), not variant-level, so this evidence
keys on gene symbol rather than chrom/pos/ref/alt -- kept as its own
dataclass module rather than folded into ClinGen's, since the two
evidence sources answer different questions (gene-disease *validity*
vs. gene-phenotype *association*) even though both are gene-level.

All dataclasses are plain, JSON-serializable (via `to_dict()`) and have
no network/IO dependency, matching every other evidence-source model
module in this codebase.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HPOPhenotypeAssociation:
    """
    One gene-phenotype-disease association row, as HPO's official
    Gene-to-Phenotype annotation file (`genes_to_phenotype.txt`)
    reports it: one HPO term, observed in the context of one specific
    disease this gene causes.

    `frequency` is HPO's own reported observation frequency for this
    phenotype in this disease -- either a fraction string ("15/15"),
    an HPO frequency-term id (e.g. "HP:0040283" = "Occasional"), or
    "-" (not reported) verbatim from the source file; never
    reinterpreted into a synthetic percentage here.
    """

    gene_symbol: str
    hpo_id: str
    hpo_name: str
    disease_id: Optional[str] = None
    frequency: Optional[str] = None
    ncbi_gene_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gene_symbol": self.gene_symbol,
            "hpo_id": self.hpo_id,
            "hpo_name": self.hpo_name,
            "disease_id": self.disease_id,
            "frequency": self.frequency,
            "ncbi_gene_id": self.ncbi_gene_id,
        }


@dataclass
class HPODiseaseAssociation:
    """One gene-disease association, as the JAX live API's per-gene annotation endpoint reports it."""

    disease_id: str
    disease_name: str
    mondo_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"disease_id": self.disease_id, "disease_name": self.disease_name, "mondo_id": self.mondo_id}


@dataclass
class HPOGeneEvidence:
    """
    Complete HPO evidence record for one gene, as returned by a
    provider (local dataset or live API) after parsing. This is the
    object `HPOLookup` (see `lookup.py`) hands back to the orchestrator.
    """

    gene_symbol: str
    source: str  # "local_dataset" | "api" | "cache"
    found: bool = False

    phenotype_associations: List[HPOPhenotypeAssociation] = field(default_factory=list)
    disease_associations: List[HPODiseaseAssociation] = field(default_factory=list)

    ncbi_gene_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def distinct_phenotype_terms(self) -> List[Dict[str, str]]:
        """Deduplicated HPO term list (id + name only), collapsing the per-disease rows the local dataset reports one term per disease as."""
        seen: Dict[str, str] = {}
        for assoc in self.phenotype_associations:
            seen.setdefault(assoc.hpo_id, assoc.hpo_name)
        return [{"hpo_id": hpo_id, "hpo_name": name} for hpo_id, name in seen.items()]

    @property
    def distinct_disease_ids(self) -> List[str]:
        ids = {a.disease_id for a in self.phenotype_associations if a.disease_id}
        ids |= {d.disease_id for d in self.disease_associations if d.disease_id}
        return sorted(ids)

    @property
    def phenotype_count(self) -> int:
        return len(self.distinct_phenotype_terms)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "source": self.source,
            "gene_symbol": self.gene_symbol,
            "ncbi_gene_id": self.ncbi_gene_id,
            "phenotype_associations": [a.to_dict() for a in self.phenotype_associations],
            "distinct_phenotype_terms": self.distinct_phenotype_terms,
            "phenotype_count": self.phenotype_count,
            "disease_associations": [d.to_dict() for d in self.disease_associations],
            "distinct_disease_ids": self.distinct_disease_ids,
            "error": self.error,
        }

    @staticmethod
    def not_found(gene_symbol: str, source: str) -> "HPOGeneEvidence":
        return HPOGeneEvidence(gene_symbol=gene_symbol, source=source, found=False)

    @staticmethod
    def from_error(gene_symbol: str, error: str) -> "HPOGeneEvidence":
        return HPOGeneEvidence(gene_symbol=gene_symbol, source="error", found=False, error=error)
