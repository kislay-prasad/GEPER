"""
Data model for Orphanet gene-disorder association evidence.

Same shape convention as `pipeline/hpo/models.py`: Orphanet's
gene-disorder curation is gene-level (a gene is associated with one or
more rare disorders, each via a specific relationship type), not
variant-level, so this evidence keys on gene symbol rather than
chrom/pos/ref/alt. Kept as its own dataclass module rather than folded
into ClinGen's or HPO's -- Orphanet answers a distinct question (which
specific rare *disorders*, by ORPHAcode, is this gene tied to, and by
what kind of relationship) from ClinGen's graded gene-disease
*clinical validity* classification (Definitive/Strong/.../Refuted) or
HPO's phenotype-term annotation.

All dataclasses are plain, JSON-serializable (via `to_dict()`) and have
no network/IO dependency, matching every other evidence-source model
module in this codebase.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OrphanetDisorderAssociation:
    """
    One gene-disorder association row, as Orphanet's official
    `en_product6.xml` ("genes associated with rare diseases") dataset
    reports it: one ORPHAcode-identified disorder, the kind of
    relationship this gene has to it, and Orphanet's own (coarse)
    validation status for that specific association.

    `association_type` is Orphanet's own controlled-vocabulary label
    for *what kind* of relationship this is (e.g. "Disease-causing
    germline mutation(s) in", "Disease-causing germline mutation(s)
    (loss of function) in", "Major susceptibility factor in",
    "Candidate gene tested in", "Disease-causing somatic mutation(s)
    in") -- never collapsed into a single yes/no "associated" flag,
    since PM1/PP1-style reasoning about a gene-disease relationship
    depends on which of these it actually is (a "candidate gene
    tested" association is a much weaker claim than a curated
    "disease-causing... loss of function" one).

    `association_status` is Orphanet's own review-status label for
    this specific association ("Assessed" | "Not yet assessed" as
    observed live -- see `tests/test_orphanet_live_fetch.py`). This is
    a binary curation-workflow flag, NOT a graded evidence-strength
    scale -- unlike ClinGen's SVI gene-disease validity classification
    (Definitive/Strong/Moderate/Limited/Disputed/Refuted/No Known
    Disease Relationship), it does not distinguish a well-established
    relationship from a weak-but-reviewed one, nor does it have a
    "disputed"/"refuted" negative category at all. See
    `pipeline/acmg_rules.py::ACMGRuleEngine._pp1`/`_bs4`'s docstrings
    for why this means Orphanet's status field cannot substitute for
    ClinGen's classification as those criteria's prerequisite check.
    """

    gene_symbol: str
    orpha_code: str
    disorder_name: str
    association_type: Optional[str] = None
    association_status: Optional[str] = None
    source_of_validation: Optional[str] = None  # e.g. a PMID reference, verbatim from the dataset

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orpha_code": self.orpha_code,
            "disorder_name": self.disorder_name,
            "association_type": self.association_type,
            "association_status": self.association_status,
            "source_of_validation": self.source_of_validation,
        }


@dataclass
class OrphanetGeneEvidence:
    """
    Complete Orphanet evidence record for one gene, as returned by
    `LocalDatasetOrphanetProvider` after parsing. This is the object
    `OrphanetLookup` (see `lookup.py`) hands back to the orchestrator.
    """

    gene_symbol: str
    source: str  # "local_dataset" | "cache" | "error"
    found: bool = False

    disorder_associations: List[OrphanetDisorderAssociation] = field(default_factory=list)
    data_version: Optional[str] = None  # Orphanet knowledge base release date, for the required citation
    error: Optional[str] = None

    @property
    def distinct_orpha_codes(self) -> List[str]:
        return sorted({a.orpha_code for a in self.disorder_associations if a.orpha_code})

    @property
    def disorder_count(self) -> int:
        return len(self.distinct_orpha_codes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "source": self.source,
            "gene_symbol": self.gene_symbol,
            "disorder_associations": [a.to_dict() for a in self.disorder_associations],
            "distinct_orpha_codes": self.distinct_orpha_codes,
            "disorder_count": self.disorder_count,
            "data_version": self.data_version,
            # Required by Orphadata Science's CC BY 4.0 licence terms
            # (https://www.orphadata.com, "Legal notice": attribution +
            # data version must accompany any reuse of the data).
            "citation": (
                f"Orphadata Science: Free access data from Orphanet. (c) INSERM 1999. "
                f"Available on http://sciences.orphadata.com/. Data version {self.data_version or 'unknown'}."
            ),
            "error": self.error,
        }

    @staticmethod
    def not_found(gene_symbol: str, source: str, data_version: Optional[str] = None) -> "OrphanetGeneEvidence":
        return OrphanetGeneEvidence(gene_symbol=gene_symbol, source=source, found=False, data_version=data_version)

    @staticmethod
    def from_error(gene_symbol: str, error: str) -> "OrphanetGeneEvidence":
        return OrphanetGeneEvidence(gene_symbol=gene_symbol, source="error", found=False, error=error)
