"""
1000 Genomes Project South Asian (SAS) sub-population frequency -- the
SOLE source for the "Indian Population Frequency" report section as of
2026-08-08.

Originally built as an honest fallback for when IndiGenomes
(`annotation/indigenomes.py`) was confirmed offline for a given run.
IndiGenomes itself was retired from GEPER's active query path entirely
on 2026-08-08 (see `DATA_SOURCE_LICENSE_AUDIT.md`: its own terms state
it "is intended for purely research purposes" and that "commercial use
... would require licensing," which GEPER has not obtained) -- so what
was a conditional fallback is now the unconditional, only source this
section has, run for every variant regardless of any other service's
availability. `annotation/indigenomes.py` is kept in the codebase, not
deleted, so IndiGenomes can be reinstated later if a commercial license
is obtained; until then, `pipeline/orchestrator.py` never calls it.

Investigation this is built from (2026-08-08, live-verified)
=======================================================================
Ensembl's own REST API (`rest.ensembl.org`, already a GEPER dependency
via `pipeline/pvs1/lookup.py`/`pipeline/clingen/utils.py`) exposes real,
live, per-sub-population 1000 Genomes phase_3 allele frequencies with no
new external service, no login, no access barrier:

  `GET /variation/human/{rsID}?pops=1` returns a `populations` list
  including entries like `{"population":
  "1000GENOMES:phase_3:GIH", "allele": "G", "allele_count": 122,
  "frequency": 0.592233...}` for every one of GIH/PJL/BEB/STU/ITU, plus
  the pooled `1000GENOMES:phase_3:SAS` figure -- confirmed live against
  rs699 (real numbers: BEB 117/172, GIH 122/206, ITU 127/204, PJL
  117/192, STU 139/204 G-allele counts/totals).

This endpoint is keyed by rsID, not by chrom:pos:ref:alt, so getting
from GEPER's native coordinate a variant is queried by requires a first
resolution step. Two candidate approaches were investigated:

  1. `database/dbsnp_client.py` -- GEPER's existing rsID resolver, but
     it queries NCBI E-utilities, a distinct external service with its
     own independent uptime (already separately HEALTH-tracked as
     "dbSNP"). Routing this fallback's resolution step through it would
     couple "IndiGenomes is down, use the fallback" to a THIRD external
     service's availability, undermining the whole point of a fallback
     meant to be resilient.
  2. Ensembl's own `overlap/region/human/{chrom}:{pos}-{pos}?feature=
     variation` endpoint -- returns candidate rsIDs at a position with
     their `alleles` (`[ref, alt, ...]`), confirmed live (e.g. rs699 ->
     `{"id": "rs699", "alleles": ["A", "G"], "start": 230710048, ...}`).
     Chosen: this keeps the entire fallback chain -- resolution AND
     population lookup -- inside the one already-integrated, already-
     HEALTH-tracked Ensembl dependency, with no new external service.

Best of all: `pipeline/orchestrator.py` already runs the dbSNP stage
before the IndiGenomes stage every variant, so when dbSNP DID resolve an
rsID this run (`dbsnp_result["found"]` and `dbsnp_result["rsid"]`),
`ThousandGenomesSASLookup.query_variant`'s `rsid_hint` parameter lets the
orchestrator hand that over directly -- zero extra network calls beyond
the population lookup itself. The `overlap/region` resolution above is
only reached when dbSNP genuinely has no rsID for this position (or
wasn't queried this run), as a self-contained, Ensembl-only fallback of
the fallback.

Reliability honesty
=======================================================================
This module reuses the "Ensembl" `utils/service_health.py` HEALTH entry
every other Ensembl-dependent stage already shares (see
`config.py::ThousandGenomesSASConfig`'s docstring) -- deliberately NOT a
new, separately-tracked service, since it genuinely is the same host.
`utils/service_health.py`'s own module docstring records Ensembl
throwing intermittent 500/502/503s in this exact session; this fallback
inherits that, not a cleaner reliability story just because it has a
different name from IndiGenomes.

Mandatory disclosures
=======================================================================
`SAMPLE_SIZES`/`TOTAL_SAMPLE_SIZE` and `_POPULATION_LABELS` below exist
specifically so the report layer never shows a bare population code
(a clinician-facing report reading "GIH" with no context is worse than
useless) and never omits the sample-size or diaspora-origin caveat --
see `report/clinical_report_builder.py::_indian_population_frequency`
for where both are rendered.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)

# The five 1000 Genomes SAS sub-populations, with clinician-readable
# labels -- real descriptions confirmed live 2026-08-08 against
# ftp.1000genomes.ebi.ac.uk/vol1/ftp/README_populations.md and
# phase3/20131219.populations.tsv. Deliberately NOT "Indian" for all
# five: GIH/ITU/STU are diaspora cohorts (Houston TX / UK / UK), and PJL
# (Pakistan) / BEB (Bangladesh) are South Asian but not Indian at all --
# stating each one's real origin honestly is the whole point of this
# module existing instead of just labeling everything "South Asian".
_POPULATION_LABELS: Dict[str, str] = {
    "GIH": "Gujarati Indian in Houston, TX, USA (diaspora, not India-resident)",
    "PJL": "Punjabi in Lahore, Pakistan (not an Indian cohort)",
    "BEB": "Bengali in Bangladesh (not an Indian cohort)",
    "STU": "Sri Lankan Tamil in the UK (diaspora, not India-resident, not Indian)",
    "ITU": "Indian Telugu in the UK (diaspora, not India-resident)",
}

# Real phase_3 (2015) sample counts per sub-population, confirmed live
# against phase3/20131219.populations.tsv's "Final Phase Samples"
# column. `TOTAL_SAMPLE_SIZE` is their sum -- both are surfaced in every
# report mention of this fallback (see this module's docstring).
SAMPLE_SIZES: Dict[str, int] = {"GIH": 106, "PJL": 96, "BEB": 86, "STU": 103, "ITU": 103}
TOTAL_SAMPLE_SIZE = sum(SAMPLE_SIZES.values())  # 494

_SUB_POPULATION_CODES = tuple(SAMPLE_SIZES.keys())
_POOLED_CODE = "SAS"
_PHASE3_PREFIX = "1000GENOMES:phase_3:"

# The two mandatory disclosures (see this module's docstring) as a
# single shared source of truth -- both `report/report_generator.py`
# (Markdown) and `report/summary.py` (PDF) import and render this exact
# text, rather than each independently paraphrasing it, so the two
# output formats can never drift on wording for a disclosure this
# important.
SAMPLE_SIZE_DISCLOSURE = (
    f"Based on only {TOTAL_SAMPLE_SIZE} total samples across all five sub-populations "
    "(1000 Genomes phase_3, 2015) -- much smaller than IndiGenomes' 1000+ India-resident genomes."
)
DIASPORA_DISCLOSURE = (
    "Samples were collected from diaspora populations outside India (e.g. Gujarati Indian in "
    "Houston TX, Indian Telugu in the UK), not from India-resident individuals."
)


def _rest_base() -> str:
    return CONFIG.api.ENSEMBL_REST_BASE.rstrip("/")


def _get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    GET `url`, retrying transient failures -- same shape
    `pipeline/pvs1/lookup.py`'s Ensembl calls already use, sharing the
    same "Ensembl" HEALTH entry (see this module's docstring for why
    that's deliberate, not an oversight).
    """
    if HEALTH.is_offline("Ensembl"):
        HEALTH.note_skip("Ensembl")
        raise ExternalAPIError(
            f"1000 Genomes SAS lookup via '{url}' skipped: Ensembl was confirmed offline at startup."
        )

    last_error: Optional[Exception] = None
    for attempt in range(1, CONFIG.thousand_genomes_sas.MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Content-Type": "application/json"},
                timeout=CONFIG.thousand_genomes_sas.QUERY_TIMEOUT_SECS,
            )
            response.raise_for_status()
            payload = response.json()
            HEALTH.note_success("Ensembl")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(f"1000 Genomes SAS lookup attempt {attempt} failed for '{url}': {exc}")
            if not is_transient_http_error(exc):
                break
            if attempt < CONFIG.thousand_genomes_sas.MAX_RETRIES:
                time.sleep(CONFIG.thousand_genomes_sas.RETRY_BACKOFF_SECS * attempt)
    HEALTH.note_failure("Ensembl")
    raise ExternalAPIError(
        f"1000 Genomes SAS lookup via '{url}' failed after {CONFIG.thousand_genomes_sas.MAX_RETRIES} attempts: {last_error}"
    )


def _resolve_rsid(chrom: str, pos: int, ref: str, alt: str) -> Optional[str]:
    """
    Ensembl-only rsID resolution for a chrom:pos:ref:alt, used only when
    no `rsid_hint` was supplied (see this module's docstring for why
    this deliberately does not call `database/dbsnp_client.py`).

    Allele-aware, never position-alone: a candidate only counts as a
    match if its own `alleles` list -- `[ref, alt, ...]`, confirmed live
    -- agrees with the queried ref/alt at the queried position, mirroring
    `database/dbsnp_client.py::DbSNPClient._variant_match`'s discipline.
    Returns None (not a guess) when nothing matches, including for
    indels whose anchor-base offset this simple check does not attempt
    to reconcile -- scoped to the common SNV case a fallback needs, not
    a full re-implementation of dbSNP's own allele normalization.
    """
    bare_chrom = chrom[3:] if chrom.lower().startswith("chr") else chrom
    url = f"{_rest_base()}/overlap/region/human/{bare_chrom}:{pos}-{pos}"
    try:
        candidates = _get(url, {"feature": "variation", "content-type": "application/json"})
    except ExternalAPIError:
        raise
    if not isinstance(candidates, list):
        return None

    ref_upper, alt_upper = ref.upper(), alt.upper()
    for candidate in candidates:
        if candidate.get("start") != pos:
            continue
        alleles = candidate.get("alleles") or []
        if len(alleles) < 2:
            continue
        if str(alleles[0]).upper() != ref_upper:
            continue
        if alt_upper in {str(a).upper() for a in alleles[1:]}:
            rsid = candidate.get("id")
            if isinstance(rsid, str) and rsid.startswith("rs"):
                return rsid
    return None


def _fetch_population_frequencies(rsid: str, alt: str) -> Dict[str, Dict[str, Any]]:
    """
    `GET /variation/human/{rsid}?pops=1`, reshaped into
    `{code: {"af", "ac", "an"}}` for the pooled `SAS` super-population
    and each of the five sub-population codes.

    Each population reports one row per allele it observed at this
    site (e.g. one `A` row and one `G` row for a biallelic SNV) -- `an`
    (allele number, total called alleles) is `sum(allele_count across
    every row for that population)`, confirmed live (GIH: A
    allele_count=84, G allele_count=122 -> an=206). `af`/`ac` are taken
    from whichever row's own `allele` field matches the query's `alt`
    allele specifically -- NOT positionally (e.g. "second row"): a real
    live check (rs699) showed the API does not guarantee ref-then-alt
    row order -- ITU's rows came back G-first/A-second while every
    other population for the same rsID came back A-first/G-second.
    Picking `entries[1]` unconditionally silently returned ITU's REF
    frequency mislabeled as its ALT frequency; explicit allele matching
    is the fix, and is the same "match the allele, never assume row
    order" discipline `database/dbsnp_client.py::DbSNPClient
    ._variant_match` and `database/clinvar_client.py` already apply to
    their own APIs.

    A population code absent from the response (can happen for a
    monomorphic/unobserved site in that specific cohort), or present
    but with no row for this exact `alt` allele, is simply absent from
    the returned dict -- never fabricated as zero, since "this cohort
    was never polymorphic here" and "this cohort's data is missing" are
    different facts a report should not conflate.
    """
    payload = _get(f"{_rest_base()}/variation/human/{rsid}", {"pops": "1", "content-type": "application/json"})
    populations = payload.get("populations") or []
    alt_upper = alt.upper()

    by_code: Dict[str, list] = {}
    for entry in populations:
        pop_label = str(entry.get("population") or "")
        if not pop_label.startswith(_PHASE3_PREFIX):
            continue
        code = pop_label[len(_PHASE3_PREFIX) :]
        if code != _POOLED_CODE and code not in _SUB_POPULATION_CODES:
            continue
        by_code.setdefault(code, []).append(entry)

    result: Dict[str, Dict[str, Any]] = {}
    for code, entries in by_code.items():
        total_ac = sum(int(e.get("allele_count") or 0) for e in entries)
        alt_entry = next((e for e in entries if str(e.get("allele") or "").upper() == alt_upper), None)
        if alt_entry is None:
            continue
        result[code] = {"af": alt_entry.get("frequency"), "ac": int(alt_entry.get("allele_count") or 0), "an": total_ac}
    return result


class ThousandGenomesSASLookup:
    """
    Orchestrator-facing facade, same `query_variant(...)` contract as
    `annotation/indigenomes.py::IndiGenomesLookup`/`pipeline.pvs1.lookup
    .TranscriptLookup` -- never raises; a genuinely unexpected failure
    is reported inside the returned dict, not thrown.
    """

    def query_variant(
        self,
        variant: Variant,
        assembly: Optional[str] = None,
        rsid_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        `rsid_hint`: an already-resolved rsID from this run's dbSNP
        stage (`dbsnp_result["rsid"]` when `dbsnp_result["found"]`) --
        see this module's docstring for why passing it avoids a
        redundant resolution call. When omitted (or the hint itself
        turns out not to carry frequency data for this position),
        `_resolve_rsid` is used instead.
        """
        if not CONFIG.thousand_genomes_sas.ENABLED:
            return {
                "skipped": True,
                "reason": "1000 Genomes SAS fallback disabled via GEPER_ENABLE_1000GENOMES_SAS=false",
                "found": False,
            }

        rsid = rsid_hint
        try:
            if not rsid:
                rsid = _resolve_rsid(variant.chrom, variant.pos, variant.ref, variant.alt)
            if not rsid:
                return {"skipped": False, "found": False, "rsid": None}

            frequencies = _fetch_population_frequencies(rsid, variant.alt)
        except ExternalAPIError as exc:
            logger.warning(f"1000 Genomes SAS lookup failed for {variant.chrom}:{variant.pos}: {exc}")
            return {"skipped": False, "found": False, "error": str(exc)}

        pooled = frequencies.get(_POOLED_CODE)
        sub_populations = {code: frequencies[code] for code in _SUB_POPULATION_CODES if code in frequencies}

        if pooled is None and not sub_populations:
            return {"skipped": False, "found": False, "rsid": rsid}

        return {
            "skipped": False,
            "found": True,
            "rsid": rsid,
            "sas_pooled": pooled,
            "sub_populations": sub_populations,
            "population_labels": {code: _POPULATION_LABELS[code] for code in sub_populations},
            "sample_sizes": {code: SAMPLE_SIZES[code] for code in sub_populations},
            "total_sample_size": TOTAL_SAMPLE_SIZE,
        }
