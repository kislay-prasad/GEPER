"""
Shared, dependency-free helpers for the UniProt integration:
gene-symbol normalization/cache keys and parsing UniProt REST API
JSON entries into `UniProtAnnotation`/`UniProtFeature`. No network, no
torch -- pure functions, trivially unit-testable in isolation, the
same shape as `pipeline/gnomad/utils.py` / `pipeline/clingen/utils.py`.
"""

from typing import Any, Dict, List, Optional

from pipeline.uniprot.models import UniProtAnnotation, UniProtFeature


def normalize_gene_symbol(symbol: Optional[str]) -> Optional[str]:
    """UniProt (like HGNC/ClinGen) reports gene symbols upper-cased; normalize any caller input the same way."""
    if not symbol:
        return None
    return symbol.strip().upper() or None


def gene_cache_key(gene_symbol: str) -> str:
    return f"gene:{normalize_gene_symbol(gene_symbol)}"


# ---------------------------------------------------------------------------
# Parsing UniProt REST API JSON entries
# ---------------------------------------------------------------------------
#
# Field names below match UniProt's current REST API entry schema
# (https://www.uniprot.org/help/return_fields,
# https://rest.uniprot.org/uniprotkb/search) -- a single "entry"
# object as returned inside the `results` list of a
# `/uniprotkb/search` response, or as returned directly by
# `/uniprotkb/{accession}`.

# UniProtKB comment types that map onto GEPER's "disease relevance"
# requirement; DISEASE is the primary one, INVOLVEMENT_IN_DISEASE is
# the current schema's field name.
_DISEASE_COMMENT_TYPES = {"DISEASE", "INVOLVEMENT_IN_DISEASE"}


def _entry_type_is_reviewed(entry: Dict[str, Any]) -> bool:
    entry_type = (entry.get("entryType") or "").lower()
    if "unreviewed" in entry_type:
        return False
    return "swiss-prot" in entry_type or "reviewed" in entry_type


def _extract_function_text(comments: List[Dict[str, Any]]) -> Optional[str]:
    for comment in comments:
        if comment.get("commentType") == "FUNCTION":
            texts = comment.get("texts") or []
            if texts:
                return texts[0].get("value")
    return None


def _extract_disease_comments(comments: List[Dict[str, Any]]) -> List[str]:
    disease_comments: List[str] = []
    for comment in comments:
        if comment.get("commentType") not in _DISEASE_COMMENT_TYPES:
            continue
        disease = comment.get("disease") or {}
        label = disease.get("diseaseId") or disease.get("diseaseAccession")
        description = disease.get("description")
        if label and description:
            disease_comments.append(f"{label}: {description}")
        elif description:
            disease_comments.append(description)
        elif label:
            disease_comments.append(label)
    return disease_comments


def _extract_features(features: List[Dict[str, Any]]) -> List[UniProtFeature]:
    result: List[UniProtFeature] = []
    for feat in features:
        location = feat.get("location") or {}
        start = (location.get("start") or {}).get("value")
        end = (location.get("end") or {}).get("value")
        result.append(
            UniProtFeature(
                feature_type=feat.get("type") or "Unknown",
                description=feat.get("description"),
                begin=start,
                end=end,
            )
        )
    return result


def parse_uniprot_entry(gene_symbol: str, entry: Dict[str, Any], source: str) -> UniProtAnnotation:
    """Parse one UniProt REST API entry object into a `UniProtAnnotation`."""
    accession = entry.get("primaryAccession")
    entry_name = entry.get("uniProtkbId")

    protein_description = entry.get("proteinDescription") or {}
    recommended = protein_description.get("recommendedName") or {}
    protein_name = (recommended.get("fullName") or {}).get("value")
    if not protein_name:
        submitted = protein_description.get("submissionNames") or []
        if submitted:
            protein_name = (submitted[0].get("fullName") or {}).get("value")

    organism = (entry.get("organism") or {}).get("scientificName")
    sequence_length = (entry.get("sequence") or {}).get("length")

    comments = entry.get("comments") or []
    features = entry.get("features") or []

    return UniProtAnnotation(
        gene_symbol=gene_symbol,
        source=source,
        found=True,
        accession=accession,
        entry_name=entry_name,
        protein_name=protein_name,
        organism=organism,
        reviewed=_entry_type_is_reviewed(entry),
        sequence_length=sequence_length,
        function_text=_extract_function_text(comments),
        disease_comments=_extract_disease_comments(comments),
        features=_extract_features(features),
    )
