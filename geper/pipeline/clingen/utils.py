"""
Shared, mostly dependency-free helpers for the ClinGen integration:
gene-symbol normalization, resolving a variant's gene symbol via
Ensembl (ClinGen curation is gene-level, so a variant must first be
mapped to a gene before any ClinGen evidence can be looked up), and
parsing ClinGen's curated-download row formats.

The one networked helper here (`resolve_gene_symbol_detail`)
deliberately reuses `CONFIG.api.ENSEMBL_REST_BASE` and the same
`overlap/region` endpoint `pipeline/models/mmsplice/service.py`
already queries for exon annotation -- this repo's established
pattern for genome annotation lookups without a pysam/cyvcf2/gffutils
dependency -- just with `feature=gene` instead of `feature=exon`.

Allele-aware gene resolution (the fifth instance of a "first-record-
wins" bug found in this codebase, after ClinVar's record selection,
ClinVar's own retrieval narrowing, and dbSNP's rsID resolution -- see
`database/clinvar_client.py` and `database/dbsnp_client.py`'s module
docstrings for the first four)
------------------------------------------------------------------
An earlier version of this module's `resolve_gene_symbol` took
`(coding or features)[0]` from Ensembl's overlap/region response --
the first protein-coding gene Ensembl happened to return at a
position, with zero disambiguation when more than one genuinely
overlaps. This is not hypothetical: live-confirmed, GRCh38 19:1228350
(inside the real, clinically significant Peutz-Jeghers gene STK11's
last exon) returns BOTH `STK11` (1177558-1228431, +strand) and
`CBARP` (1228282-1239465, -strand), two independent protein-coding
genes on opposite strands. Worse than the ClinVar/dbSNP cases: this
result is resolved ONCE per variant and then *reused* -- by HPO,
Orphanet, the PVS1 transcript-structure lookup, and the entire
UniProt -> InterPro -> AlphaFold chain (see `pipeline/orchestrator.py`
-- `_run_hpo_stage`/`_run_orphanet_stage`/`_run_transcript_stage`/
`_run_uniprot_stage` all reuse `clingen_result['gene_symbol']` rather
than re-resolving) -- so a wrong pick here silently corrupts every
gene-level evidence item in the report at once, not just one field.

Resolution order, each a `GeneResolutionStatus`-tagged outcome so a
caller never has to guess whether `gene_symbol=None` means "nothing
here" or "something here we refused to guess about":
  1. The VCF's own `GENE=` INFO field, when present, is trusted
     outright (`RESOLVED`, `source="vcf_gene_info"`) -- the upstream
     annotator has already done transcript-aware gene assignment; a
     coordinate-only overlap lookup has no basis to second-guess it,
     and skipping the Ensembl call entirely is also strictly cheaper.
  2. Otherwise, Ensembl overlap/region, filtered to `protein_coding`.
     Zero candidates -> `NOT_FOUND`. Exactly one -> `RESOLVED`
     (`source="ensembl_single_candidate"`), same as before.
  3. More than one protein-coding candidate: disambiguate by which
     candidate's own coding sequence (not just its gene-body span,
     which can include megabases of intron/UTR) actually contains the
     position, via the same transcript-structure machinery PVS1/PM4/
     PS1/PM5 already trust (`pipeline.pvs1.lookup.TranscriptLookup`).
     Exactly one CDS-containing candidate -> `RESOLVED`
     (`source="ensembl_cds_containment"`). Otherwise, MANE Select
     status among the tied candidates is tried as a second tie-break
     (`source="ensembl_mane_select"`). If a single winner still can't
     be determined, this is `AMBIGUOUS` -- not a coin flip, not a
     silent guess -- with every tied candidate symbol named in
     `GeneResolution.candidates` and `.reason` for the caller to
     surface honestly.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
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


class GeneResolutionStatus(str, enum.Enum):
    """
    Mirrors `database/clinvar_client.py::ClinVarMatchStatus` and
    `database/dbsnp_client.py::DbSNPMatchStatus`'s tri-state pattern --
    its own type for the same reason those two are separate from each
    other (`pipeline/stage_schemas.py::StageStatus` /
    `pipeline/provenance.py::VersionStatus` precedent): independent
    resources with independent failure/ambiguity shapes.
    """

    NOT_FOUND = "not_found"    # no protein-coding gene overlaps this position, and no VCF GENE= hint
    AMBIGUOUS = "ambiguous"    # multiple candidate genes overlap and could not be disambiguated
    RESOLVED = "resolved"      # exactly one gene determined


@dataclass(frozen=True)
class GeneResolution:
    """Full result of resolving a variant's gene -- see this module's docstring for the resolution order."""

    status: GeneResolutionStatus
    gene_symbol: Optional[str]
    source: str  # "vcf_gene_info" | "ensembl_single_candidate" | "ensembl_cds_containment" | "ensembl_mane_select" | "none"
    reason: str
    candidates: List[str] = field(default_factory=list)  # every viable candidate symbol seen, populated for AMBIGUOUS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "gene_symbol": self.gene_symbol,
            "source": self.source,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


def _fetch_overlapping_genes(chrom: str, pos: int, build: str) -> Optional[List[Dict[str, Any]]]:
    """Raw Ensembl `overlap/region?feature=gene` fetch. Returns None (distinct from an empty list) only on a genuine request failure."""
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
            return features if isinstance(features, list) else []
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(f"Ensembl gene-overlap lookup attempt {attempt} failed for {bare_chrom}:{pos}: {exc}")
            if attempt < CONFIG.clingen.MAX_RETRIES:
                time.sleep(CONFIG.clingen.RETRY_BACKOFF_SECS * attempt)
    logger.warning(
        f"Ensembl gene-overlap lookup failed for {bare_chrom}:{pos} after "
        f"{CONFIG.clingen.MAX_RETRIES} attempts: {last_error}"
    )
    return None


def resolve_gene_symbol_detail(
    chrom: str, pos: int, build: str = "GRCh38", vcf_gene_hint: Optional[str] = None,
) -> GeneResolution:
    """
    Full-detail gene resolution -- see this module's docstring for the
    resolution order (VCF `GENE=` hint, then Ensembl overlap with
    CDS-containment/MANE-Select disambiguation). Never raises and
    never guesses: an unresolvable position is `NOT_FOUND`, a
    genuinely tied position is `AMBIGUOUS`, and `gene_symbol` is None
    for both -- callers must branch on `.status`, not just truthiness,
    to report *why* rather than silently proceeding.
    """
    hint = normalize_gene_symbol(vcf_gene_hint)
    if hint:
        return GeneResolution(
            GeneResolutionStatus.RESOLVED, hint, "vcf_gene_info",
            reason=f"Used the VCF's own GENE={hint} annotation directly; no Ensembl overlap lookup was needed.",
        )

    if CONFIG.clingen.OFFLINE_MODE:
        return GeneResolution(GeneResolutionStatus.NOT_FOUND, None, "none", reason="offline mode; no gene-overlap lookup performed.")

    features = _fetch_overlapping_genes(chrom, pos, build)
    if features is None:
        return GeneResolution(GeneResolutionStatus.NOT_FOUND, None, "none", reason="Ensembl gene-overlap lookup failed.")
    if not features:
        return GeneResolution(GeneResolutionStatus.NOT_FOUND, None, "none", reason="no gene overlaps this position.")

    # A position can overlap more than one annotated gene (overlapping
    # transcripts on opposite strands, nested genes) -- protein_coding
    # entries are preferred over non-coding ones (pseudogenes, lncRNAs,
    # antisense transcripts), but multiple protein_coding candidates
    # can still remain and must not be resolved by response order.
    coding = [f for f in features if f.get("biotype") == "protein_coding"]
    candidates = coding or features
    symbols: List[str] = []
    for f in candidates:
        symbol = normalize_gene_symbol(f.get("external_name") or f.get("gene_id"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    if not symbols:
        return GeneResolution(GeneResolutionStatus.NOT_FOUND, None, "none", reason="no protein-coding gene overlaps this position.")
    if len(symbols) == 1:
        return GeneResolution(
            GeneResolutionStatus.RESOLVED, symbols[0], "ensembl_single_candidate",
            reason=f"Exactly one protein-coding gene ({symbols[0]}) overlaps this position.",
        )

    return _disambiguate_overlapping_genes(symbols, pos, build)


def _disambiguate_overlapping_genes(symbols: List[str], pos: int, build: str) -> GeneResolution:
    """
    Second-stage disambiguation for >1 protein-coding candidate at one
    position: prefer whichever candidate's own coding sequence
    actually contains `pos` (a gene-body overlap can be pure
    intron/UTR of a much larger neighboring gene, which the position
    filter above cannot distinguish on its own), falling back to MANE
    Select status among any still-tied candidates. Reuses
    `pipeline.pvs1.lookup.TranscriptLookup` -- the same cached,
    already-tested Ensembl transcript-structure fetch PVS1/PM4/PS1/PM5
    rely on -- rather than a second, bespoke Ensembl interaction;
    imported locally to avoid a module-level import-order dependency
    between `pipeline.clingen` and `pipeline.pvs1` (pvs1 itself has no
    reverse dependency on clingen, so this is a one-way, non-circular
    reuse, not a cycle).
    """
    from pipeline.pvs1.lookup import TranscriptLookup
    from pipeline.pvs1.utils import transcript_context_from_dict

    lookup = TranscriptLookup()
    cds_hits: List[str] = []
    mane_hits: List[str] = []
    for symbol in symbols:
        try:
            result = lookup.query_gene(symbol, build=build)
        except Exception as exc:  # noqa: BLE001 - disambiguation is best-effort; a lookup hiccup falls through to AMBIGUOUS, never a guess
            logger.warning(f"Transcript lookup for gene-overlap disambiguation failed for '{symbol}': {exc}")
            continue
        record = (result or {}).get("transcript")
        transcript = transcript_context_from_dict(record) if record else None
        if transcript is None:
            continue
        if transcript.cds_position(pos) is not None:
            cds_hits.append(symbol)
        if transcript.is_mane_select:
            mane_hits.append(symbol)

    if len(cds_hits) == 1:
        others = ", ".join(s for s in symbols if s != cds_hits[0])
        return GeneResolution(
            GeneResolutionStatus.RESOLVED, cds_hits[0], "ensembl_cds_containment",
            reason=(
                f"Position falls within {cds_hits[0]}'s coding sequence but not the other overlapping "
                f"candidate('s) canonical transcript ({others})."
            ),
            candidates=symbols,
        )

    tie_pool = cds_hits if len(cds_hits) > 1 else symbols
    mane_in_pool = [s for s in mane_hits if s in tie_pool]
    if len(mane_in_pool) == 1:
        return GeneResolution(
            GeneResolutionStatus.RESOLVED, mane_in_pool[0], "ensembl_mane_select",
            reason=f"{mane_in_pool[0]} is the MANE Select gene among the tied candidates ({', '.join(tie_pool)}).",
            candidates=symbols,
        )

    return GeneResolution(
        GeneResolutionStatus.AMBIGUOUS, None, "none",
        reason=(
            f"{len(symbols)} protein-coding genes genuinely overlap this position ({', '.join(symbols)}) "
            "and could not be disambiguated by CDS containment or MANE Select status."
        ),
        candidates=symbols,
    )


def resolve_gene_symbol(chrom: str, pos: int, build: str = "GRCh38") -> Optional[str]:
    """
    Backward-compatible convenience wrapper over
    `resolve_gene_symbol_detail` for callers that only need the plain
    symbol (or None). Returns None for BOTH `NOT_FOUND` and
    `AMBIGUOUS` -- by design, this never guesses -- so any caller that
    cares about *why* (to report a clear reason rather than a bare
    "no gene" downstream) should call `resolve_gene_symbol_detail`
    directly instead, as `pipeline/clingen/lookup.py::ClinGenLookup`
    and `pipeline/uniprot/lookup.py::UniProtLookup` both now do.
    """
    return resolve_gene_symbol_detail(chrom, pos, build=build).gene_symbol


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
