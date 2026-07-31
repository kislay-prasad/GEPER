"""
dbSNP client.

Looks up rsID and population/frequency annotation for a variant using
NCBI's E-utilities (esearch against the 'snp' database) plus the NCBI
Variation Services REST API for richer per-rsID detail once an rsID is
known.
"""

import time
from typing import Any, Dict, Optional

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.ncbi_eutils import lenient_json_loads, parse_retry_after, warn_if_placeholder_contact

logger = get_logger(__name__)


class DbSNPClient:
    """Fetches rsID and variant annotation from NCBI dbSNP."""

    def __init__(self):
        self.eutils_base = CONFIG.api.NCBI_EUTILS_BASE
        self.variation_base = CONFIG.api.NCBI_VARIATION_BASE
        self.db = CONFIG.api.DBSNP_DB
        warn_if_placeholder_contact(CONFIG.api.NCBI_EMAIL, caller="DbSNPClient")

    def lookup_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        """
        Find the rsID(s) matching a chrom/pos/ref/alt, then fetch detail.

        `assembly` is the genome build the *input VCF's coordinates* are
        on (e.g. "GRCh37" or "GRCh38"), as resolved by the pipeline's
        assembly preflight. dbSNP's Entrez position search is
        build-specific -- the bare `[chrpos]`/`POSITION` field is always
        GRCh38 coordinates, regardless of what build the caller has in
        mind, and a GRCh37 coordinate must instead be searched with the
        distinct `POSITION_GRCH37` field. Passing a GRCh37 position
        through the GRCh38-only field silently returns zero hits for
        almost every variant (the two builds' coordinates only rarely
        coincide), which is indistinguishable from "not in dbSNP" unless
        this is accounted for explicitly.
        """
        # If the VCF itself already carries an rsID (common: ID column
        # starts with 'rs'), skip the search step entirely.
        if variant.variant_id and variant.variant_id.startswith("rs"):
            rsid = variant.variant_id
            detail = self._fetch_variation_detail(rsid)
            return {"rsid": rsid, "found": True, "source": "vcf_id_column", "detail": detail}

        chrom = variant.chrom.replace("chr", "")
        position_field = self._position_field(assembly)
        term = f"{chrom}[CHR] AND {variant.pos}[{position_field}]"
        uids = self._esearch(term)

        if not uids:
            logger.info(f"No dbSNP record found for '{term}'.")
            return {"rsid": None, "found": False, "source": "esearch", "detail": None}

        rsid = f"rs{uids[0]}"
        detail = self._fetch_variation_detail(rsid)
        return {"rsid": rsid, "found": True, "source": "esearch", "detail": detail}

    @staticmethod
    def _position_field(assembly: Optional[str]) -> str:
        """
        Map a resolved genome build to dbSNP's Entrez position search
        field. GRCh37 needs the explicit legacy-assembly field;
        everything else (including an unresolved/unknown build) falls
        back to the current-assembly field, which is dbSNP's own
        default and the best guess absent better information.
        """
        if assembly and assembly.strip().upper().replace("-", "").replace("_", "") in {
            "GRCH37",
            "HG19",
            "B37",
        }:
            return "POSITION_GRCH37"
        return "POSITION"

    def _esearch(self, term: str) -> list:
        params = {
            "db": self.db,
            "term": term,
            "retmode": "json",
            "retmax": 5,
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        payload = self._request_json(f"{self.eutils_base}/esearch.fcgi", params)
        return payload.get("esearchresult", {}).get("idlist", [])

    def _fetch_variation_detail(self, rsid: str) -> Optional[Dict[str, Any]]:
        """Fetch richer detail (alleles, frequency, gene context) for a known rsID."""
        numeric_id = rsid.lstrip("rs")
        url = f"{self.variation_base}/refsnp/{numeric_id}"
        try:
            payload = self._request_json(url, params={})
        except ExternalAPIError as exc:
            logger.warning(f"Could not fetch dbSNP variation detail for {rsid}: {exc}")
            return None

        primary_snapshot = payload.get("primary_snapshot_data", {})
        alleles = primary_snapshot.get("placements_with_allele", [])
        gene_names = set()
        for placement in alleles:
            for allele_annotation in placement.get("alleles", []):
                assembly_info = allele_annotation.get("hgvs", "")
                if assembly_info:
                    gene_names.add(assembly_info.split(":")[0])

        return {
            "rsid": rsid,
            "genes": sorted(gene_names) if gene_names else None,
            "mane_select": primary_snapshot.get("canonical_annotation") is not None,
            "raw_present": bool(primary_snapshot),
        }

    def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        GET `url` and return the parsed JSON body, retrying up to
        `CONFIG.api.MAX_RETRIES` times. Shared with `_fetch_variation_detail`
        (NCBI Variation Services, a different host/API from classic
        eutils) -- the retry/parse behavior below is generic HTTP
        robustness, not eutils-specific, so it applies to both call
        sites.

        A 429 (eutils' rate-limit response) honors NCBI's `Retry-After`
        header instead of guessing via the default exponential
        backoff -- see `database/clinvar_client.py::_request_json`'s
        docstring (this mirrors that fix). The JSON body is parsed
        leniently (`lenient_json_loads`) rather than via
        `response.json()`'s strict parser, for the same
        control-character reason documented there.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.api.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.api.REQUEST_TIMEOUT_SECS)
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    wait = retry_after if retry_after is not None else CONFIG.api.RETRY_BACKOFF_SECS * attempt
                    last_error = requests.HTTPError(f"429 Too Many Requests from '{url}'")
                    logger.warning(
                        f"dbSNP request attempt {attempt} rate-limited (429); waiting {wait:.1f}s "
                        f"({'NCBI Retry-After header' if retry_after is not None else 'default backoff'})."
                    )
                    if attempt < CONFIG.api.MAX_RETRIES:
                        time.sleep(wait)
                    continue
                response.raise_for_status()
                return lenient_json_loads(response.text, source=url)
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"dbSNP request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(
            f"dbSNP request to '{url}' failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}"
        )
