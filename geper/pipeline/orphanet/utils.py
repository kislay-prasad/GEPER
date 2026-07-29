"""
Shared, dependency-free helpers for the Orphanet integration:
gene-symbol normalization, and streaming-parsing Orphanet's official
`en_product6.xml` ("genes associated with rare diseases") dataset into
a gene-symbol-indexed map of `OrphanetDisorderAssociation` rows.

No network, no torch -- pure functions/parsing, matching every other
evidence-source utils module in this codebase.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from pipeline.orphanet.models import OrphanetDisorderAssociation


def normalize_gene_symbol(symbol: Optional[str]) -> Optional[str]:
    """Orphanet reports gene symbols upper-cased (HGNC convention), matching ClinGen's/HPO's own normalization."""
    if not symbol:
        return None
    return symbol.strip().upper() or None


def gene_cache_key(gene_symbol: str) -> str:
    return f"gene:{normalize_gene_symbol(gene_symbol)}"


# ---------------------------------------------------------------------------
# Parsing Orphanet's `en_product6.xml` ("genes associated with rare diseases")
# ---------------------------------------------------------------------------
#
# Real schema, verified against a live download of
# https://www.orphadata.com/data/xml/en_product6.xml (root `<JDBOR
# date="..." version="...">`, 4245 `<Disorder>` entries as of the July
# 2026 release -- see tests/test_orphanet_live_fetch.py):
#
#   <JDBOR date="..." version="...">
#     <DisorderList count="...">
#       <Disorder id="...">
#         <OrphaCode>168829</OrphaCode>
#         <Name lang="en">Primary peritoneal carcinoma</Name>
#         <DisorderGeneAssociationList count="...">
#           <DisorderGeneAssociation>
#             <SourceOfValidation>22006311[PMID]</SourceOfValidation>
#             <Gene id="...">
#               <Name lang="en">BRCA1 DNA repair associated</Name>
#               <Symbol>BRCA1</Symbol>
#               ...
#             </Gene>
#             <DisorderGeneAssociationType id="...">
#               <Name lang="en">Major susceptibility factor in</Name>
#             </DisorderGeneAssociationType>
#             <DisorderGeneAssociationStatus id="...">
#               <Name lang="en">Assessed</Name>
#             </DisorderGeneAssociationStatus>
#           </DisorderGeneAssociation>
#         </DisorderGeneAssociationList>
#       </Disorder>
#       ...
#
# Parsed with `iterparse` + `elem.clear()` per `Disorder` rather than
# `ET.parse()` -- the real file is ~22MB and this codebase's
# convention (see `pipeline/hpo/provider.py`, `pipeline/clingen/
# provider.py`) is to keep local-dataset loading's peak memory
# proportional to one record, not the whole file.


def parse_gene_disorder_xml(path: str) -> "tuple[Dict[str, List[OrphanetDisorderAssociation]], Optional[str]]":
    """
    Stream-parse Orphanet's `en_product6.xml` into a `{gene_symbol: [OrphanetDisorderAssociation, ...]}`
    index, plus the dataset's release date (the `date` attribute of
    the root `<JDBOR>` element, used for the required citation's "Data
    version"). Never raises on a malformed individual record -- skips
    it and continues, matching this codebase's graceful-degradation
    convention for bulk dataset parsing.
    """
    by_gene: Dict[str, List[OrphanetDisorderAssociation]] = {}
    data_version: Optional[str] = None

    context = ET.iterparse(path, events=("start", "end"))
    for event, elem in context:
        if event == "start" and elem.tag == "JDBOR":
            data_version = elem.get("date")
            continue
        if event != "end" or elem.tag != "Disorder":
            continue

        try:
            orpha_code = (elem.findtext("OrphaCode") or "").strip()
            disorder_name = (elem.findtext("Name") or "").strip()
            if not orpha_code or not disorder_name:
                continue

            for assoc in elem.iter("DisorderGeneAssociation"):
                gene_elem = assoc.find("Gene")
                symbol = normalize_gene_symbol(gene_elem.findtext("Symbol") if gene_elem is not None else None)
                if not symbol:
                    continue

                assoc_type = assoc.findtext("DisorderGeneAssociationType/Name")
                assoc_status = assoc.findtext("DisorderGeneAssociationStatus/Name")
                source_of_validation = (assoc.findtext("SourceOfValidation") or "").strip() or None

                by_gene.setdefault(symbol, []).append(
                    OrphanetDisorderAssociation(
                        gene_symbol=symbol,
                        orpha_code=orpha_code,
                        disorder_name=disorder_name,
                        association_type=(assoc_type or "").strip() or None,
                        association_status=(assoc_status or "").strip() or None,
                        source_of_validation=source_of_validation,
                    )
                )
        finally:
            # Free this Disorder subtree's memory now that its
            # associations are extracted -- the whole point of
            # iterparse over ET.parse for a 22MB file.
            elem.clear()

    return by_gene, data_version
