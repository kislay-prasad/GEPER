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

import json
import logging
import re
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Patient-phenotype input (--hpo-terms / --phenotype-file) -> `phenotype_result`
# ---------------------------------------------------------------------------
#
# `ACMGRuleEngine._pp4` has always been fully implemented and reads
# `phenotype_result["hpo_term_ids"]` (see acmg_rules.py); the only thing
# that ever kept PP4 permanently "not_evaluated" was that nothing in
# GEPER's CLI/orchestrator ever collected or populated that dict. These
# helpers are that missing input mechanism: they turn the two supported
# CLI inputs into the exact dict shape `_pp4` already expects, validating
# and warning on malformed HPO IDs without ever raising.

_HPO_ID_PATTERN = re.compile(r"^HP:\d{7}$")


def is_well_formed_hpo_id(term: str) -> bool:
    """True if `term` looks like a well-formed HPO ID, e.g. 'HP:0001250'
    (the 'HP:' prefix plus exactly 7 digits, per HPO's own ID spec)."""
    return bool(_HPO_ID_PATTERN.match((term or "").strip()))


def _validate_and_dedupe(raw_terms: List[str], source: str, logger: Optional[logging.Logger]) -> List[str]:
    """Strips, validates, and deduplicates a list of raw HPO ID strings
    from `source` (used only for the warning message). A malformed term
    is logged as a warning and dropped -- never raised -- so one typo in
    a clinician-supplied list doesn't take down the whole run."""
    seen = set()
    valid_terms: List[str] = []
    for raw in raw_terms:
        term = (raw or "").strip()
        if not term:
            continue
        if not is_well_formed_hpo_id(term):
            if logger is not None:
                logger.warning(
                    f"Ignoring malformed HPO term '{term}' from {source} -- "
                    "expected the form 'HP:#######' (7 digits). Continuing "
                    "with the remaining terms."
                )
            continue
        if term not in seen:
            seen.add(term)
            valid_terms.append(term)
    return valid_terms


def parse_hpo_terms_arg(hpo_terms: Optional[str], logger: Optional[logging.Logger] = None) -> List[str]:
    """Parses a comma-separated `--hpo-terms` CLI value (e.g.
    'HP:0001250,HP:0002011') into a validated, deduplicated list of HPO
    IDs. Returns an empty list for None/empty input."""
    if not hpo_terms:
        return []
    raw_terms = hpo_terms.split(",")
    return _validate_and_dedupe(raw_terms, "--hpo-terms", logger)


def load_phenotype_file(path: str, logger: Optional[logging.Logger] = None) -> List[str]:
    """
    Loads patient-observed HPO terms from a `--phenotype-file`: either a
    JSON file containing a list of HPO ID strings, or a plain text file
    with one HPO ID per line. A missing/unreadable file, malformed JSON,
    or an individual malformed term is logged as a warning and skipped
    rather than raised -- the same graceful-degradation convention as
    every other optional input file in this pipeline (e.g.
    `--patient-meta`, see `report/summary.py::_parse_patient_meta`).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        if logger is not None:
            logger.warning(f"Could not read --phenotype-file '{path}' ({exc}); ignoring it.")
        return []

    stripped = content.strip()
    if not stripped:
        return []

    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON list of HPO ID strings")
            raw_terms = [str(item) for item in parsed]
        except (ValueError, TypeError) as exc:
            if logger is not None:
                logger.warning(f"--phenotype-file '{path}' is not a valid JSON list of HPO IDs ({exc}); ignoring it.")
            return []
    else:
        raw_terms = stripped.splitlines()

    return _validate_and_dedupe(raw_terms, f"--phenotype-file '{path}'", logger)


def build_phenotype_result(
    hpo_terms: Optional[str] = None,
    phenotype_file: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """
    Combines `--hpo-terms` and `--phenotype-file` into the
    `phenotype_result` dict `ACMGRuleEngine._pp4` already expects
    (`{"hpo_term_ids": [...]}` -- see acmg_rules.py). Returns `None`
    when neither input was supplied at all, so
    `InterpretationEngine.interpret(phenotype_result=None)` behaves
    exactly as every prior run already does: PP4 stays "not_evaluated".
    """
    if not hpo_terms and not phenotype_file:
        return None

    terms = parse_hpo_terms_arg(hpo_terms, logger)
    if phenotype_file:
        for term in load_phenotype_file(phenotype_file, logger):
            if term not in terms:
                terms.append(term)

    return {"hpo_term_ids": terms}
