"""
ClinVar client.

Queries NCBI ClinVar via the public E-utilities API (esearch +
esummary) for clinical significance annotations on a variant,
identified either by rsID or by a chrom/pos/ref/alt HGVS-style query.
"""

import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class ClinVarClient:
    """Fetches clinical significance data for a variant from NCBI ClinVar."""

    def __init__(self):
        self.base_url = CONFIG.api.NCBI_EUTILS_BASE
        self.db = CONFIG.api.CLINVAR_DB

    def query_variant(
        self, variant: Variant, rsid: Optional[str] = None, assembly: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Look up ClinVar records for a variant. Prefers an rsID (from a
        prior dbSNP lookup) for a precise match; otherwise falls back
        to a positional search term.

        `assembly` is the genome build the input VCF's coordinates are
        on. ClinVar's Entrez position field is build-specific
        (`chrpos37` vs `chrpos38`); the bare `chrpos` field's default
        build is not something client code should rely on, so the
        build-qualified field is always used explicitly once the
        pipeline has resolved a build.
        """
        search_term = self._build_search_term(variant, rsid, assembly)
        uids = self._esearch(search_term)

        if not uids:
            logger.info(f"No ClinVar records found for '{search_term}'.")
            return {
                "query": search_term,
                "found": False,
                "records": [],
            }

        records = self._esummary(uids, variant)
        return {
            "query": search_term,
            "found": True,
            "record_count": len(records),
            "records": records,
        }

    def _build_search_term(
        self, variant: Variant, rsid: Optional[str], assembly: Optional[str]
    ) -> str:
        if rsid:
            return f"{rsid}[rs]"
        chrom = variant.chrom.replace("chr", "")
        position_field = self._position_field(assembly)
        # NOTE: earlier versions appended a bare `AND {ref}>{alt}` clause
        # here with no Entrez field tag. An untagged term is matched as
        # free text against ClinVar's default search fields (title,
        # etc.), which is unreliable: many variant titles use protein/
        # cDNA HGVS notation rather than a literal genomic "REF>ALT"
        # string, minus-strand genes show the complemented bases, and
        # indels don't have a "REF>ALT" form at all. That silently
        # dropped real matches, indistinguishable from "not in ClinVar".
        # Chrom+build-qualified position is normally sufficient to
        # locate the record; ref/alt agreement is instead checked
        # after the fact in `_esummary` (see `ref_alt_match`) so
        # multi-allelic sites can still be flagged without the risk of
        # a bad text filter discarding a true hit.
        return f"{chrom}[chr] AND {variant.pos}[{position_field}]"

    @staticmethod
    def _position_field(assembly: Optional[str]) -> str:
        """
        Map a resolved genome build to ClinVar's Entrez position field.
        The bare `chrpos` field's default build is undocumented/
        ambiguous, so the build-qualified field (`chrpos37` /
        `chrpos38`) is always used explicitly. Falls back to GRCh38
        (ClinVar's current-assembly field) when the build is unresolved,
        matching dbSNP's fallback for consistency.
        """
        if assembly and assembly.strip().upper().replace("-", "").replace("_", "") in {
            "GRCH37",
            "HG19",
            "B37",
        }:
            return "chrpos37"
        return "chrpos38"

    def _esearch(self, term: str) -> list:
        params = {
            "db": self.db,
            "term": term,
            "retmode": "json",
            "retmax": 10,
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY

        payload = self._request_json(f"{self.base_url}/esearch.fcgi", params)
        return payload.get("esearchresult", {}).get("idlist", [])

    def _esummary(self, uids: list, variant: Optional[Variant] = None) -> list:
        params = {
            "db": self.db,
            "id": ",".join(uids),
            "retmode": "json",
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY

        payload = self._request_json(f"{self.base_url}/esummary.fcgi", params)
        result = payload.get("result", {})
        records = []
        for uid in result.get("uids", []):
            entry = result.get(uid, {})
            germline = entry.get("germline_classification", {})
            records.append(
                {
                    "uid": uid,
                    "title": entry.get("title"),
                    "clinical_significance": germline.get("description"),
                    "review_status": germline.get("review_status"),
                    "last_evaluated": germline.get("last_evaluated"),
                    "condition": [
                        trait.get("trait_name")
                        for trait in germline.get("trait_set", [])
                        if isinstance(trait, dict)
                    ],
                    "accession": entry.get("accession"),
                    "ref_alt_match": self._check_ref_alt(entry, variant),
                }
            )
        return records

    @staticmethod
    def _check_ref_alt(entry: Dict[str, Any], variant: Optional[Variant]) -> Optional[bool]:
        """
        Best-effort agreement check between the queried variant's
        ref/alt and the allele ClinVar actually returned, using the
        `variation_set[].canonical_spdi` field esummary provides (SPDI
        format: `seq:pos:ref:alt`). Only meaningful for a positional
        (non-rsID) lookup where more than one allele can share a
        position; returns None (not applicable/undetermined) rather
        than False when the data needed to compare isn't present, so
        callers never treat "couldn't check" as "definitely wrong".
        """
        if variant is None:
            return None
        variation_set = entry.get("variation_set")
        if not isinstance(variation_set, list):
            return None
        for v in variation_set:
            spdi = v.get("canonical_spdi") if isinstance(v, dict) else None
            if not spdi or spdi.count(":") < 3:
                continue
            _, _, spdi_ref, spdi_alt = spdi.split(":", 3)
            if spdi_ref.upper() == variant.ref.upper() and spdi_alt.upper() == variant.alt.upper():
                return True
        return False if variation_set else None

    def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.api.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.api.REQUEST_TIMEOUT_SECS)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"ClinVar request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(
            f"ClinVar request to '{url}' failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}"
        )
