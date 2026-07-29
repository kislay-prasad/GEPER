"""
Data model for ClinGen evidence.

Kept deliberately separate from `pipeline/vcf_parser.py::Variant` and
from the per-variant evidence shapes already in this codebase
(`pipeline/gnomad/models.py::GnomadAnnotation`, AlphaMissense's
result dict, etc.) for the same reason those are separate: ClinGen
curation is fundamentally *gene-level* (a gene-disease pair, or a
gene's dosage-sensitivity score), not variant-level, so its evidence
shape has a different key -- gene symbol -- rather than
chrom/pos/ref/alt.

All dataclasses are plain, JSON-serializable (via `to_dict()`) and
have no network/IO dependency whatsoever, matching every other
evidence-source model module in this codebase.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ClinGen's Gene-Disease Clinical Validity classification tiers, in
# strength order (weakest first). Source: ClinGen Gene-Disease
# Validity Standard Operating Procedure (SOP), the "Classification"
# column of ClinGen's curated gene-validity downloads
# (https://search.clinicalgenome.org/kb/gene-validity). "No Known
# Disease Relationship" and "Disputed"/"Refuted" are not a strength of
# *positive* evidence, but are kept in the same ordered tuple so
# `classification_rank()` below has one consistent total order to sort
# against.
CLINICAL_VALIDITY_CLASSIFICATIONS = (
    "No Known Disease Relationship",
    "Disputed",
    "Refuted",
    "Limited",
    "Moderate",
    "Strong",
    "Definitive",
)

# Numeric strength used only for "pick the strongest curated
# classification for this gene" comparisons -- Refuted/Disputed sort
# below Limited deliberately (they are not weaker *positive* evidence,
# they are *contrary* evidence), so this is a rank for internal
# tie-breaking only, never displayed to a user directly.
_CLASSIFICATION_RANK: Dict[str, int] = {
    "Refuted": -2,
    "Disputed": -1,
    "No Known Disease Relationship": 0,
    "Limited": 1,
    "Moderate": 2,
    "Strong": 3,
    "Definitive": 4,
}


def classification_rank(classification: Optional[str]) -> int:
    """Numeric strength for a Gene-Disease Validity classification string; unknown/None -> 0."""
    if not classification:
        return 0
    return _CLASSIFICATION_RANK.get(classification.strip(), 0)


# ClinGen Dosage Sensitivity curation scores (haploinsufficiency /
# triplosensitivity), per ClinGen's Dosage Sensitivity Curation SOP.
# These integer codes are what ClinGen's own dosage-sensitivity
# downloads/API report directly in the "Score" column; GEPER never
# invents its own scale, only labels ClinGen's.
DOSAGE_SCORE_LABELS: Dict[int, str] = {
    0: "No evidence available",
    1: "Little evidence for dosage pathogenicity",
    2: "Some evidence for dosage pathogenicity",
    3: "Sufficient evidence for dosage pathogenicity",
    30: "Gene associated with autosomal recessive phenotype",
    40: "Dosage sensitivity unlikely",
}

# Scores that count as "sufficient evidence" for PVS1-style reasoning
# -- i.e. loss-of-function is an established disease mechanism for
# this gene. Kept as an explicit, named constant (rather than a
# magic "== 3") so the ACMG-integration code that reads it is
# self-documenting.
DOSAGE_SUFFICIENT_EVIDENCE_SCORE = 3
DOSAGE_UNLIKELY_SCORE = 40


@dataclass
class GeneDiseaseValidity:
    """One ClinGen Gene-Disease Clinical Validity curation (one gene-disease pair)."""

    gene_symbol: str
    disease_label: str
    disease_id: Optional[str] = None  # MONDO/OMIM identifier, e.g. "MONDO:0007947"
    classification: Optional[str] = None  # one of CLINICAL_VALIDITY_CLASSIFICATIONS
    moi: Optional[str] = None  # mode of inheritance, e.g. "Autosomal dominant"
    sop_version: Optional[str] = None
    gcep: Optional[str] = None  # curating Gene Curation Expert Panel
    classification_date: Optional[str] = None
    online_report_url: Optional[str] = None
    gene_id: Optional[str] = None  # HGNC identifier, e.g. "HGNC:1100" (from the "GENE ID (HGNC)" download column)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gene_symbol": self.gene_symbol,
            "disease_label": self.disease_label,
            "disease_id": self.disease_id,
            "classification": self.classification,
            "moi": self.moi,
            "sop_version": self.sop_version,
            "gcep": self.gcep,
            "classification_date": self.classification_date,
            "online_report_url": self.online_report_url,
            "gene_id": self.gene_id,
        }


@dataclass
class DosageSensitivity:
    """ClinGen Dosage Sensitivity curation for one gene (haploinsufficiency + triplosensitivity)."""

    gene_symbol: str
    haploinsufficiency_score: Optional[int] = None
    haploinsufficiency_description: Optional[str] = None
    triplosensitivity_score: Optional[int] = None
    triplosensitivity_description: Optional[str] = None

    @property
    def haploinsufficiency_label(self) -> Optional[str]:
        if self.haploinsufficiency_score is None:
            return None
        return DOSAGE_SCORE_LABELS.get(self.haploinsufficiency_score, f"Score {self.haploinsufficiency_score}")

    @property
    def triplosensitivity_label(self) -> Optional[str]:
        if self.triplosensitivity_score is None:
            return None
        return DOSAGE_SCORE_LABELS.get(self.triplosensitivity_score, f"Score {self.triplosensitivity_score}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gene_symbol": self.gene_symbol,
            "haploinsufficiency_score": self.haploinsufficiency_score,
            "haploinsufficiency_label": self.haploinsufficiency_label,
            "haploinsufficiency_description": self.haploinsufficiency_description,
            "triplosensitivity_score": self.triplosensitivity_score,
            "triplosensitivity_label": self.triplosensitivity_label,
            "triplosensitivity_description": self.triplosensitivity_description,
        }


@dataclass
class Actionability:
    """ClinGen Clinical Actionability summary for one gene (adult and/or pediatric context)."""

    gene_symbol: str
    disease_label: Optional[str] = None
    adult_actionability_score: Optional[float] = None
    pediatric_actionability_score: Optional[float] = None
    report_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gene_symbol": self.gene_symbol,
            "disease_label": self.disease_label,
            "adult_actionability_score": self.adult_actionability_score,
            "pediatric_actionability_score": self.pediatric_actionability_score,
            "report_url": self.report_url,
        }


@dataclass
class ClinGenGeneEvidence:
    """
    Complete ClinGen evidence record for one gene, as returned by a
    provider (local curated-dataset or live API) after parsing. This
    is the object `ClinGenLookup` (see `lookup.py`) hands back to the
    orchestrator, and what `AnnotatedVariant.clingen` (see
    `pipeline/vcf_parser.py`) stores per variant.
    """

    gene_symbol: str
    source: str  # "local_dataset" | "api" | "cache"
    found: bool = False

    gene_disease_validities: List[GeneDiseaseValidity] = field(default_factory=list)
    dosage_sensitivity: Optional[DosageSensitivity] = None
    actionability: List[Actionability] = field(default_factory=list)

    clingen_gene_id: Optional[str] = None  # ClinGen's own gene identifier (e.g. "CGGV:...")
    last_updated: Optional[str] = None
    error: Optional[str] = None

    @property
    def strongest_validity(self) -> Optional[GeneDiseaseValidity]:
        """The single strongest-classified gene-disease curation for this gene, if any."""
        if not self.gene_disease_validities:
            return None
        return max(self.gene_disease_validities, key=lambda v: classification_rank(v.classification))

    @property
    def clinical_validity_summary(self) -> Optional[str]:
        top = self.strongest_validity
        return top.classification if top else None

    @property
    def expert_panel_summary(self) -> Optional[str]:
        top = self.strongest_validity
        return top.gcep if top else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "source": self.source,
            "gene_symbol": self.gene_symbol,
            "clingen_gene_id": self.clingen_gene_id,
            "gene_disease_validity": [v.to_dict() for v in self.gene_disease_validities],
            "clinical_validity_summary": self.clinical_validity_summary,
            "expert_panel": self.expert_panel_summary,
            "dosage_sensitivity": self.dosage_sensitivity.to_dict() if self.dosage_sensitivity else None,
            "actionability": [a.to_dict() for a in self.actionability],
            "last_updated": self.last_updated,
            "error": self.error,
        }

    @staticmethod
    def not_found(gene_symbol: str, source: str) -> "ClinGenGeneEvidence":
        return ClinGenGeneEvidence(gene_symbol=gene_symbol, source=source, found=False)

    @staticmethod
    def from_error(gene_symbol: str, error: str) -> "ClinGenGeneEvidence":
        return ClinGenGeneEvidence(gene_symbol=gene_symbol, source="error", found=False, error=error)
