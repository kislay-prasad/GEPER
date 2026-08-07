"""
Data model for PS3/BS3 functional-evidence records.

One `FunctionalEvidenceRecord` is one already-adjudicated PS3/BS3 call
(ClinGen Evidence Repository) or one bucketed raw functional-assay
score (MaveDB) for a *specific* variant -- unlike ClinGen/HPO/Orphanet,
which are gene-level, functional evidence answers a variant-level
question ("does *this* substitution behave abnormally in *this*
assay?"), so there is no separate variant->gene resolution step here;
the caller (`pipeline/orchestrator.py::_run_functional_evidence_stage`)
already has the variant's genomic and coding HGVS strings on hand.

All dataclasses are plain, JSON-serializable (via `to_dict()`) and have
no network/IO dependency, matching every other evidence-source model
module in this codebase.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FunctionalEvidenceRecord:
    """
    One PS3- or BS3-relevant functional-assay finding for a specific
    variant, from one source.

    `call` is "PS3" or "BS3" -- which direction this record argues
    for. `strength` is an ACMG/AMP strength tier ("strong" |
    "moderate" | "supporting"), either the source's own explicit
    assignment (ClinGen ERepo's VCEP-specified strength, e.g. an
    evidence code literally labeled "PS3_Moderate") or a
    `CONFIG.functional_evidence`-configured default when the source
    gives a bare Met/abnormal call with no strength of its own.
    """

    source: str  # "clingen_erepo" | "mavedb"
    call: str  # "PS3" | "BS3"
    strength: str  # "strong" | "moderate" | "supporting"
    matched_hgvs: str
    expert_panel: Optional[str] = None
    specification_url: Optional[str] = None
    classification_outcome: Optional[str] = None
    condition: Optional[str] = None
    raw_score: Optional[float] = None
    score_set_urn: Optional[str] = None
    functional_classification: Optional[str] = None  # MaveDB only: "normal" | "abnormal" | "not_specified"
    research_use_only: Optional[bool] = None  # MaveDB only
    publication: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "call": self.call,
            "strength": self.strength,
            "matched_hgvs": self.matched_hgvs,
            "expert_panel": self.expert_panel,
            "specification_url": self.specification_url,
            "classification_outcome": self.classification_outcome,
            "condition": self.condition,
            "raw_score": self.raw_score,
            "score_set_urn": self.score_set_urn,
            "functional_classification": self.functional_classification,
            "research_use_only": self.research_use_only,
            "publication": self.publication,
        }


@dataclass
class FunctionalEvidenceResult:
    """
    Complete functional-evidence record for one variant, as returned by
    `FunctionalEvidenceLookup.query_variant` after both sources have
    been consulted (MaveDB only when ClinGen ERepo had nothing for this
    exact variant -- see `lookup.py`).
    """

    gene_symbol: Optional[str]
    source: str  # "clingen_erepo" | "mavedb" | "none"
    found: bool = False
    records: List[FunctionalEvidenceRecord] = field(default_factory=list)
    error: Optional[str] = None
    # Names ("ClinGen ERepo" / "MaveDB") of sources that were skipped
    # this run because `utils/service_health.py::HEALTH` had already
    # confirmed them offline, as opposed to a source that was actually
    # queried and simply failed/found nothing -- see `lookup.py`'s
    # `_match_erepo`/`_match_mavedb`, the only place this is populated.
    # A clinician reading "not evaluated" for PS3/BS3 needs to be able
    # to tell "neither source had data for this variant" apart from
    # "this source was never queried this run" -- they are not the
    # same finding.
    unavailable_sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "source": self.source,
            "gene_symbol": self.gene_symbol,
            "records": [r.to_dict() for r in self.records],
            "error": self.error,
            "unavailable_sources": list(self.unavailable_sources),
        }

    @staticmethod
    def not_found(gene_symbol: Optional[str], source: str = "none") -> "FunctionalEvidenceResult":
        return FunctionalEvidenceResult(gene_symbol=gene_symbol, source=source, found=False)

    @staticmethod
    def from_error(gene_symbol: Optional[str], error: str) -> "FunctionalEvidenceResult":
        return FunctionalEvidenceResult(gene_symbol=gene_symbol, source="error", found=False, error=error)
