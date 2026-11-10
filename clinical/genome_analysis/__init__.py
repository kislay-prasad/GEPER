"""
clinical/genome_analysis — Phase 1: chromosome reference metadata, contig
normalisation, and pure variant-to-chromosome aggregation.

Pure functions and frozen dataclasses. No I/O, no database, no network, no
third-party imports, and no import of anything else in the repository -- it lives
under clinical/ because the clinical service image ships only clinical/ files.

WHAT THIS MODULE DOES NOT DO, AND MUST NOT BE READ AS DOING:
  * It does not produce, imply or infer a karyotype (no "46,XX"/"46,XY"), and it
    never infers biological sex from X/Y variants.
  * It does not detect or report copy-number or structural variants.
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
from .summary import (
    LIMITATIONS,
    PATIENT_WITHHELD_CHROMOSOMES,
    PATIENT_WITHHELD_NOTICE,
    AnalysisSummary,
    ChromosomeSummary,
    patient_projection,
    summarise_run_document,
)

__all__ = [
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
