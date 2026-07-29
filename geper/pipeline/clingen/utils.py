"""
Shared, mostly dependency-free helpers for the ClinGen integration:
gene-symbol normalization, resolving a variant's gene symbol via
Ensembl (ClinGen curation is gene-level, so a variant must first be
mapped to a gene before any ClinGen evidence can be looked up), and
parsing ClinGen's curated-download row formats.

The one networked helper here (`resolve_gene_symbol`) deliberately
reuses `CONFIG.api.ENSEMBL_REST_BASE` and the same `overlap/region`
endpoint `pipeline/models/mmsplice/service.py` already queries for
exon annotation -- this repo's established pattern for genome
annotation lookups without a pysam/cyvcf2/gffutils dependency -- just
with `feature=gene` instead of `feature=exon`.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.clingen.models import DosageSensitivity, GeneDiseaseValidity
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


def normalize_gene_symbol(symbol: Optional[str]) -> Optional[str]:
    """ClinGen (like HGNC) reports gene symbols upper-cased; normalize any caller input the same way."""
    if not symbol:
        return None
    return symbol.strip().upper() or None


def _species_for_build(build: str) -> str:
    # Ensembl's REST API is build-specific at the *server* level
    # (grch37.rest.ensembl.org vs rest.ensembl.org for GRCh38), not via
    # a query parameter -- see `pipeline/models/mmsplice/service.py`'s
    # same distinction. ClinGen curation itself is genome-build
    # agnostic (a gene symbol, not a coordinate), so this only affects
    # which Ensembl mirror answers the overlap/region lookup.
    return "homo_sapiens"


def resolve_gene_symbol(chrom: str, pos: int, build: str = "GRCh38") -> Optional[str]:
    """
    Resolve the HGNC gene symbol overlapping a genomic position via
    Ensembl's REST `overlap/region` endpoint (`feature=gene`), the
    same endpoint/library already used for exon annotation in
    `pipeline/models/mmsplice/service.py`. Returns None (never raises)
    if no gene overlaps the position, the lookup fails, or offline
    mode is configured -- ClinGen evidence is then simply skipped for
    that variant (requirement #9: never crash the pipeline).

    Known limitation: this queries Ensembl's GRCh38 REST endpoint for
    both builds' gene bodies, since gene *symbols* (unlike exon
    coordinates) rarely differ between builds for the same locus; a
    GRCh37-specific Ensembl mirror (`grch37.rest.ensembl.org`) can be
    substituted via `CONFIG.api.ENSEMBL_REST_BASE` override if a
    deployment needs build-exact gene-boundary resolution.
    """
    if CONFIG.clingen.OFFLINE_MODE:
        return None

    base_url = CONFIG.api.ENSEMBL_REST_BASE
    bare_chrom = chrom[3:] if chrom.lower().startswith("chr") else chrom
    url = f"{base_url}/overlap/region/{_species_for_build(build)}/{bare_chrom}:{pos}-{pos}"
    params = {"feature": "gene", "content-type": "application/json"}

    last_error: Optional[Exception] = None
    for attempt in range(1, CONFIG.clingen.MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Content-Type": "application/json"},
                timeout=CONFIG.clingen.QUERY_TIMEOUT_SECS,
            )
            response.raise_for_status()
            features = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(f"Ensembl gene-overlap lookup attempt {attempt} failed for {bare_chrom}:{pos}: {exc}")
            if attempt < CONFIG.clingen.MAX_RETRIES:
                time.sleep(CONFIG.clingen.RETRY_BACKOFF_SECS * attempt)
    else:
        logger.warning(
            f"Ensembl gene-overlap lookup failed for {bare_chrom}:{pos} after "
            f"{CONFIG.clingen.MAX_RETRIES} attempts: {last_error}"
        )
        return None

    if not isinstance(features, list) or not features:
        return None

    # A position can overlap more than one annotated gene (overlapping
    # transcripts on opposite strands, nested genes); prefer the first
    # protein-coding entry Ensembl returns, falling back to whatever
    # is first if none are explicitly flagged protein_coding.
    coding = [f for f in features if f.get("biotype") == "protein_coding"]
    chosen = (coding or features)[0]
    symbol = chosen.get("external_name") or chosen.get("gene_id")
    return normalize_gene_symbol(symbol)


def gene_cache_key(gene_symbol: str) -> str:
    return f"gene:{normalize_gene_symbol(gene_symbol)}"


# ---------------------------------------------------------------------------
# Parsing ClinGen's curated-download row formats
# ---------------------------------------------------------------------------
#
# Column names below match ClinGen's own published Gene-Disease
# Validity and Dosage Sensitivity file downloads
# (https://search.clinicalgenome.org/kb/gene-validity and
# https://search.clinicalgenome.org/kb/dosage/download, "File
# Downloads & APIs" on each curation page). Both are stable,
# versioned flat-file formats ClinGen has published for years
# specifically so consumers do not need to scrape the web UI or
# depend on an unversioned live API for routine ingestion --
# `pipeline/clingen/provider.py::LocalDatasetClinGenProvider` treats a
# deployer-provisioned copy of these files as the primary, most
# reliable evidence source, with the live API as a secondary fallback
# (see that module's docstring, and `README.md`'s ClinGen section, for
# why: this sandbox has no network route to clinicalgenome.org, so the
# live API's exact response schema could not be executed against here
# and should be re-verified against ClinGen's current API docs before
# relying on it in production).

_GENE_VALIDITY_COLUMNS = {
    "gene_symbol": ("GENE SYMBOL", "gene_symbol"),
    "gene_id": ("GENE ID (HGNC)", "gene_id", "HGNC ID"),
    "disease_label": ("DISEASE LABEL", "disease_label"),
    "disease_id": ("DISEASE ID (MONDO)", "disease_id"),
    "moi": ("MOI", "moi"),
    "sop_version": ("SOP", "sop_version"),
    "classification": ("CLASSIFICATION", "classification"),
    "online_report_url": ("ONLINE REPORT", "online_report_url"),
    "classification_date": ("CLASSIFICATION DATE", "classification_date"),
    "gcep": ("GCEP", "gcep"),
}

# Two real ClinGen dosage-sensitivity export shapes have been observed
# live (see `pipeline/clingen/bootstrap.py`'s docstring for why the
# ftp.clinicalgenome.org mirror -- the title-case column names below --
# is the one this codebase fetches by default): the interactive KB
# export uses ALLCAPS column names matching `_GENE_VALIDITY_COLUMNS`'
# convention, while the numeric-score ftp download this project
# actually depends on uses title-case names instead ("Haploinsufficiency
# Score", not "HAPLOINSUFFICIENCY SCORE"). Both are listed as candidates
# so either shape parses correctly.
_DOSAGE_COLUMNS = {
    "gene_symbol": ("GENE SYMBOL", "Gene Symbol", "gene_symbol"),
    "haploinsufficiency_score": ("HAPLOINSUFFICIENCY SCORE", "Haploinsufficiency Score", "haploinsufficiency_score"),
    "haploinsufficiency_description": (
        "HAPLOINSUFFICIENCY DESCRIPTION", "Haploinsufficiency Description", "haploinsufficiency_description",
    ),
    "triplosensitivity_score": ("TRIPLOSENSITIVITY SCORE", "Triplosensitivity Score", "triplosensitivity_score"),
    "triplosensitivity_description": (
        "TRIPLOSENSITIVITY DESCRIPTION", "Triplosensitivity Description", "triplosensitivity_description",
    ),
}


def _pick(row: Dict[str, str], candidates: tuple) -> Optional[str]:
    for name in candidates:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_gene_validity_row(row: Dict[str, str]) -> Optional[GeneDiseaseValidity]:
    """Parse one row of a ClinGen gene-validity TSV/CSV download into a `GeneDiseaseValidity`."""
    gene_symbol = normalize_gene_symbol(_pick(row, _GENE_VALIDITY_COLUMNS["gene_symbol"]))
    disease_label = _pick(row, _GENE_VALIDITY_COLUMNS["disease_label"])
    if not gene_symbol or not disease_label:
        return None
    return GeneDiseaseValidity(
        gene_symbol=gene_symbol,
        disease_label=disease_label,
        disease_id=_pick(row, _GENE_VALIDITY_COLUMNS["disease_id"]),
        classification=_pick(row, _GENE_VALIDITY_COLUMNS["classification"]),
        moi=_pick(row, _GENE_VALIDITY_COLUMNS["moi"]),
        sop_version=_pick(row, _GENE_VALIDITY_COLUMNS["sop_version"]),
        gcep=_pick(row, _GENE_VALIDITY_COLUMNS["gcep"]),
        classification_date=_pick(row, _GENE_VALIDITY_COLUMNS["classification_date"]),
        online_report_url=_pick(row, _GENE_VALIDITY_COLUMNS["online_report_url"]),
        gene_id=_pick(row, _GENE_VALIDITY_COLUMNS["gene_id"]),
    )


def parse_dosage_row(row: Dict[str, str]) -> Optional[DosageSensitivity]:
    """Parse one row of a ClinGen dosage-sensitivity TSV/CSV download into a `DosageSensitivity`."""
    gene_symbol = normalize_gene_symbol(_pick(row, _DOSAGE_COLUMNS["gene_symbol"]))
    if not gene_symbol:
        return None
    return DosageSensitivity(
        gene_symbol=gene_symbol,
        haploinsufficiency_score=_to_int(_pick(row, _DOSAGE_COLUMNS["haploinsufficiency_score"])),
        haploinsufficiency_description=_pick(row, _DOSAGE_COLUMNS["haploinsufficiency_description"]),
        triplosensitivity_score=_to_int(_pick(row, _DOSAGE_COLUMNS["triplosensitivity_score"])),
        triplosensitivity_description=_pick(row, _DOSAGE_COLUMNS["triplosensitivity_description"]),
    )
