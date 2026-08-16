"""
ClinGen Evidence Repository (ERepo) provider -- primary PS3/BS3 source.

erepo.clinicalgenome.org distributes ClinGen Variant Curation Expert
Panel (VCEP) classifications, each carrying a full ACMG/AMP
evidence-code table (PVS1, PS3, PM2, ... each "Met"/"Not Met", with an
optional strength suffix like "PS3_Moderate" when the VCEP's own
specification downgrades/upgrades the default strength). CC0-licensed,
same as every other ClinGen curated resource GEPER already integrates
(`pipeline/clingen/`).

Verified live during development (unlike `ClinGenConfig.API_ENDPOINT`
above, which this sandbox cannot reach): a real
`GET {endpoint}/classifications?gene=BRCA1` request returned real
ENIGMA BRCA1/BRCA2 VCEP curations, including
`NM_007294.4:c.135-1G>T` with evidence code `PS3: Met`. The same query
for `gene=CFTR` returned an empty `variantInterpretations` list -- a
genuine coverage gap (no VCEP has published CFTR functional-evidence
curations to ERepo as of this integration), not a request-shape bug.

One request per *gene* (not per variant): the endpoint returns every
curated variant for that gene in one response, so this provider
builds a complete per-gene index (every HGVS string ERepo lists for
each curated variant -> that variant's PS3/BS3-Met records) that the
caller (`lookup.py`) caches and reuses for every subsequent variant in
the same gene.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.functional_evidence.models import FunctionalEvidenceRecord
from pipeline.functional_evidence.utils import bare_evidence_code, parse_evidence_code_strength
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)

_RELEVANT_CODES = ("PS3", "BS3")


class ErepoFunctionalEvidenceProvider:
    """Queries the ClinGen Evidence Repository for a gene's curated PS3/BS3 calls."""

    name = "clingen_erepo"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = (endpoint or CONFIG.functional_evidence.EREPO_API_ENDPOINT).rstrip("/")

    def is_available(self) -> bool:
        return bool(CONFIG.functional_evidence.EREPO_ENABLED) and not CONFIG.functional_evidence.OFFLINE_MODE

    def fetch_gene_index(self, gene_symbol: str) -> Dict[str, List[FunctionalEvidenceRecord]]:
        """
        Returns a dict mapping every HGVS string ERepo lists for a
        curated variant of `gene_symbol` to the list of PS3/BS3-Met
        `FunctionalEvidenceRecord`s for that variant (usually 0 or 1
        entries per variant, more if multiple VCEPs curated it).
        Raises `ExternalAPIError` on a genuine request failure --
        callers are expected to catch this and record it, matching
        every other provider in this codebase.
        """
        payload = self._get(gene_symbol)
        index: Dict[str, List[FunctionalEvidenceRecord]] = {}
        for entry in payload.get("variantInterpretations", []) or []:
            hgvs_list = entry.get("hgvs") or []
            condition = (entry.get("condition") or {}).get("label")
            records = self._records_from_entry(entry, condition)
            if not records:
                continue
            for hgvs in hgvs_list:
                index.setdefault(hgvs, []).extend(records)
        return index

    @staticmethod
    def _records_from_entry(entry: Dict[str, Any], condition: Optional[str]) -> List[FunctionalEvidenceRecord]:
        records: List[FunctionalEvidenceRecord] = []
        for guideline in entry.get("guidelines") or []:
            outcome = (guideline.get("outcome") or {}).get("label")
            cspec_url = guideline.get("cspecId")
            for agent in guideline.get("agents") or []:
                expert_panel = agent.get("affiliation")
                for evidence_code in agent.get("evidenceCodes") or []:
                    label = evidence_code.get("label", "")
                    status = evidence_code.get("status", "")
                    bare = bare_evidence_code(label)
                    if bare not in _RELEVANT_CODES or status != "Met":
                        continue
                    strength = parse_evidence_code_strength(
                        label,
                        CONFIG.functional_evidence.EREPO_DEFAULT_STRENGTH,
                    )
                    records.append(
                        FunctionalEvidenceRecord(
                            source="clingen_erepo",
                            call=bare,
                            strength=strength,
                            matched_hgvs="",  # filled in by lookup.py at match time
                            expert_panel=expert_panel,
                            specification_url=cspec_url,
                            classification_outcome=outcome,
                            condition=condition,
                        )
                    )
        return records

    def _get(self, gene_symbol: str) -> Dict[str, Any]:
        url = f"{self.endpoint}/classifications"

        if HEALTH.is_offline("ClinGen ERepo"):
            HEALTH.note_skip("ClinGen ERepo")
            logger.warning(f"ClinGen ERepo request to '{url}' skipped: ClinGen ERepo was confirmed offline at startup.")
            # Round 28: the message actually raised reaches
            # `pipeline/functional_evidence/lookup.py::_match_erepo`,
            # which folds it into `errors`/the returned `error` field,
            # embedded verbatim in geper_results.json AND rendered
            # unconditionally into every Markdown report's "Stage
            # Warnings / Errors" section via `pipeline/orchestrator.py::
            # _run_functional_evidence_stage`. Must not embed `url`; full
            # detail goes to the log line above only.
            raise ExternalAPIError("ClinGen ERepo request skipped: ClinGen ERepo was confirmed offline at startup.")

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.functional_evidence.MAX_RETRIES + 1):
            try:
                response = requests.get(
                    url,
                    params={"gene": gene_symbol},
                    timeout=CONFIG.functional_evidence.QUERY_TIMEOUT_SECS,
                )
                if response.status_code == 404:
                    HEALTH.note_success("ClinGen ERepo")
                    return {"variantInterpretations": []}
                response.raise_for_status()
                result = response.json()
                HEALTH.note_success("ClinGen ERepo")
                return result
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"ClinGen ERepo request attempt {attempt} failed for gene '{gene_symbol}': {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.functional_evidence.MAX_RETRIES:
                    time.sleep(CONFIG.functional_evidence.RETRY_BACKOFF_SECS * attempt)
        HEALTH.note_failure("ClinGen ERepo")
        logger.warning(
            f"ClinGen ERepo request to '{url}' failed after "
            f"{CONFIG.functional_evidence.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: see the sanitization note on the offline-skip raise
        # above -- same reach, same fix.
        raise ExternalAPIError(f"ClinGen ERepo request failed after {CONFIG.functional_evidence.MAX_RETRIES} attempts")
