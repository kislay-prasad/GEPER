"""
Shared, mostly dependency-free helpers for the HPO integration:
gene-symbol normalization and parsing HPO's official Gene-to-Phenotype
annotation file rows.

Unlike `pipeline/clingen/utils.py::resolve_gene_symbol`, HPO evidence
needs no separate variant->gene resolution step: `pipeline/orchestrator.py`
runs the HPO stage after the ClinGen stage and reuses the gene symbol
ClinGen's own Ensembl overlap lookup already resolved for that variant
(the same reuse `_run_uniprot_stage`/`_run_transcript_stage` already do),
exactly as `pipeline/hpo/lookup.py::HPOLookup.query_variant` documents.
"""

from __future__ import annotations

from typing import Dict, Optional

from pipeline.hpo.models import HPOPhenotypeAssociation


def normalize_gene_symbol(symbol: Optional[str]) -> Optional[str]:
    """HPO's Gene-to-Phenotype file (like HGNC/ClinGen) reports gene symbols upper-cased; normalize any caller input the same way."""
    if not symbol:
        return None
    return symbol.strip().upper() or None


def gene_cache_key(gene_symbol: str) -> str:
    return f"gene:{normalize_gene_symbol(gene_symbol)}"


# ---------------------------------------------------------------------------
# Parsing HPO's official Gene-to-Phenotype annotation file
# ---------------------------------------------------------------------------
#
# Column names match HPO's real, verified-live `genes_to_phenotype.txt`
# release file (part of http://purl.obolibrary.org/obo/hp/hpoa/, the
# same release directory as `phenotype.hpoa`): a plain TSV with a
# single header row and no preamble to skip -- unlike ClinGen's two
# download shapes (see `pipeline/clingen/provider.py::_clingen_export_rows`),
# a bare `csv.DictReader` parses this file correctly as-is.

_COLUMNS = ("ncbi_gene_id", "gene_symbol", "hpo_id", "hpo_name", "frequency", "disease_id")


def parse_genes_to_phenotype_row(row: Dict[str, str]) -> Optional[HPOPhenotypeAssociation]:
    """Parse one row of HPO's `genes_to_phenotype.txt` download into an `HPOPhenotypeAssociation`."""
    gene_symbol = normalize_gene_symbol(row.get("gene_symbol"))
    hpo_id = (row.get("hpo_id") or "").strip()
    hpo_name = (row.get("hpo_name") or "").strip()
    if not gene_symbol or not hpo_id or not hpo_name:
        return None
    frequency = (row.get("frequency") or "").strip()
    disease_id = (row.get("disease_id") or "").strip()
    ncbi_gene_id = (row.get("ncbi_gene_id") or "").strip()
    return HPOPhenotypeAssociation(
        gene_symbol=gene_symbol,
        hpo_id=hpo_id,
        hpo_name=hpo_name,
        disease_id=disease_id or None,
        frequency=frequency or None,
        ncbi_gene_id=ncbi_gene_id or None,
    )
