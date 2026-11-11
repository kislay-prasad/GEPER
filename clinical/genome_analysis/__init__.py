"""
clinical/genome_analysis — Phase 1: chromosome reference metadata, contig
normalisation, and pure variant-to-chromosome aggregation.

Pure functions and frozen dataclasses. No I/O, no database, no network, no
third-party imports, and no import of anything else in the repository -- it lives
under clinical/ because the clinical service image ships only clinical/ files.

WHAT THIS MODULE DOES NOT DO, AND MUST NOT BE READ AS DOING:
  * It does not produce, imply or infer a karyotype (no "46,XX"/"46,XY"), and it
    never infers biological sex from X/Y variants.
  * It does not DETECT copy-number or structural variants. structural.py and
    structural_vcf.py validate and represent calls made by an external, validated
    caller; visualization.py lays them out.
  * It does not classify variants clinically.
"""

from .normalization import normalize_contig
from .reference import (
    CANONICAL_CHROMOSOMES,
    REFERENCE_SOURCES,
    SUPPORTED_ASSEMBLIES,
    Chromosome,
    canonical_assembly,
    chromosomes_for,
    get_chromosome,
)
from .structural import (
    BreakendPairing,
    CopyNumberVariant,
    StructuralValidationError,
    StructuralVariant,
    assign_lanes,
    pair_breakends,
    relate_cnvs,
)
from .structural_vcf import (
    CALLSET_DOCUMENT_SCHEMA_VERSION,
    CALLSET_PARSER_VERSION,
    PARSER_RULESETS,
    CallSet,
    CallSetRejected,
    RecordOutcome,
    callset_from_document,
    parse_structural_vcf,
    verify_document,
    verify_document_for_version,
)
from .summary import (
    LIMITATIONS,
    PATIENT_WITHHELD_CHROMOSOMES,
    PATIENT_WITHHELD_NOTICE,
    AnalysisSummary,
    ChromosomeSummary,
    patient_projection,
    summarise_run_document,
)
from .visualization import build_chromosome_map

__all__ = [
    "BreakendPairing",
    "CALLSET_DOCUMENT_SCHEMA_VERSION",
    "CALLSET_PARSER_VERSION",
    "PARSER_RULESETS",
    "callset_from_document",
    "verify_document_for_version",
    "verify_document",
    "CallSet",
    "CallSetRejected",
    "CopyNumberVariant",
    "RecordOutcome",
    "StructuralValidationError",
    "StructuralVariant",
    "assign_lanes",
    "build_chromosome_map",
    "pair_breakends",
    "parse_structural_vcf",
    "relate_cnvs",
    "CANONICAL_CHROMOSOMES",
    "LIMITATIONS",
    "PATIENT_WITHHELD_CHROMOSOMES",
    "PATIENT_WITHHELD_NOTICE",
    "REFERENCE_SOURCES",
    "SUPPORTED_ASSEMBLIES",
    "AnalysisSummary",
    "Chromosome",
    "ChromosomeSummary",
    "canonical_assembly",
    "chromosomes_for",
    "get_chromosome",
    "normalize_contig",
    "patient_projection",
    "summarise_run_document",
]
