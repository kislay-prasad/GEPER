"""
dbSNP client.

Looks up rsID and population/frequency annotation for a variant using
NCBI's E-utilities (esearch against the 'snp' database) plus the NCBI
Variation Services REST API for richer per-rsID detail once an rsID is
known.

Allele-aware rsID selection
----------------------------
`_esearch`'s query term is position-based (`{chrom}[CHR] AND
{pos}[POSITION]`), same shape and same limitation as
`database/clinvar_client.py`'s positional ClinVar search: dbSNP UIDs
are frequently multi-allelic *and* a single position can host more
than one distinct rsID. Confirmed live: 17:43094298 (GRCh38) returns
two -- `rs397508848`, a 2bp deletion (`c.1232_1233del`), and
`rs80357024`, the SNV actually being analyzed (`c.1233T>G`, alongside
its A>G/A>T siblings under the same rsID). An earlier version of this
client took `uids[0]` unconditionally, which resolved `rs397508848`
for that position -- the wrong rsID, silently attributed to the
queried variant in report text ("Catalogued in dbSNP as
rs397508848") and (before `database/clinvar_client.py`'s own retrieval
fix) fed straight into a ClinVar rsID search that then couldn't find
the correct ClinVar record either, since it never had the right rsID
to search with.

This mirrors `database/clinvar_client.py::ClinVarClient`'s fix in
spirit -- classify every candidate with `variant_match`, report a
tri-state status, never guess -- but is deliberately its own
`DbSNPMatchStatus` enum rather than importing `ClinVarMatchStatus`
across modules: the two clients query unrelated NCBI resources with
independent lifecycles (`pipeline/stage_schemas.py::StageStatus` and
`pipeline/provenance.py::VersionStatus` are the same kind of
intentionally-separate-but-parallel pair in this codebase, for the
same reason). The dbSNP 'snp' db esummary conveniently returns an
`spdi` field in the *same* `seq:0-based-pos:ref:alt` format ClinVar's
`canonical_spdi` uses (comma-joined when one rsID covers multiple ALT
alleles), so `_variant_match` here is the same position+allele check,
just parsing a flatter field.
"""

import enum
import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.ncbi_eutils import lenient_json_loads, parse_retry_after, warn_if_placeholder_contact
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)


class DbSNPMatchStatus(str, enum.Enum):
    """Mirrors `database/clinvar_client.py::ClinVarMatchStatus` -- see this module's docstring for why it's a separate type."""

    NOT_FOUND = "not_found"  # esearch returned nothing at this position at all
    POSITION_ONLY = "position_only"  # rsID(s) exist at this position, but none match this allele
    MATCHED = "matched"  # at least one rsID's allele (and position) matches the query variant


class DbSNPClient:
    """Fetches rsID and variant annotation from NCBI dbSNP."""

    def __init__(self):
        self.eutils_base = CONFIG.api.NCBI_EUTILS_BASE
        self.variation_base = CONFIG.api.NCBI_VARIATION_BASE
        self.db = CONFIG.api.DBSNP_DB
        warn_if_placeholder_contact(CONFIG.api.NCBI_EMAIL, caller="DbSNPClient")

    def lookup_variant(self, variant: Variant, assembly: Optional[str] = None) -> Dict[str, Any]:
        """
        Find the rsID matching a chrom/pos/ref/alt, then fetch detail.

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

        Returns `found=True` (and a non-None `rsid`/`primary_record`)
        only when a candidate's own allele/position match the *queried*
        variant -- never merely because the position search returned
        something (see this module's docstring). `records` is still
        the full, unfiltered, position-based candidate list, for
        callers that want to show other rsIDs catalogued at this
        position as context.
        """
        # If the VCF itself already carries an rsID (common: ID column
        # starts with 'rs'), skip the search step entirely. This is a
        # user/caller-asserted identifier, not independently verified
        # against ref/alt here (no position search was even run to
        # cross-check it against) -- trusted the same way it always
        # was, `variant_match: None` marks it as unverified rather than
        # silently implying it went through the same allele check as
        # an esearch-derived candidate.
        if variant.variant_id and variant.variant_id.startswith("rs"):
            rsid = variant.variant_id
            detail = self._fetch_variation_detail(rsid)
            record = {"rsid": rsid, "variant_match": None}
            return {
                "rsid": rsid,
                "found": True,
                "match_status": DbSNPMatchStatus.MATCHED.value,
                "source": "vcf_id_column",
                "detail": detail,
                "record_count": 1,
                "matched_record_count": 1,
                "records": [record],
                "matched_records": [record],
                "primary_record": record,
            }

        chrom = variant.chrom.replace("chr", "")
        position_field = self._position_field(assembly)
        term = f"{chrom}[CHR] AND {variant.pos}[{position_field}]"
        uids = self._esearch(term)

        if not uids:
            logger.info(f"No dbSNP record found for '{term}'.")
            return {
                "rsid": None,
                "found": False,
                "match_status": DbSNPMatchStatus.NOT_FOUND.value,
                "source": "esearch",
                "detail": None,
                "record_count": 0,
                "matched_record_count": 0,
                "records": [],
                "matched_records": [],
                "primary_record": None,
            }

        candidates = self._esummary(uids, variant)
        matched = [c for c in candidates if c.get("variant_match") is True]

        if matched:
            primary = self._select_primary(matched)
            match_status = DbSNPMatchStatus.MATCHED
            rsid = primary["rsid"]
            detail = self._fetch_variation_detail(rsid)
        else:
            primary = None
            match_status = DbSNPMatchStatus.POSITION_ONLY
            rsid = None
            detail = None
            logger.info(
                f"dbSNP returned {len(candidates)} record(s) for '{term}' but none match "
                f"{variant.chrom}:{variant.pos} {variant.ref}>{variant.alt} by allele -- reporting "
                "POSITION_ONLY, not attributing any of their rsIDs to this variant."
            )

        return {
            "rsid": rsid,
            "found": match_status == DbSNPMatchStatus.MATCHED,
            "match_status": match_status.value,
            "source": "esearch",
            "detail": detail,
            "record_count": len(candidates),
            "matched_record_count": len(matched),
            "records": candidates,
            "matched_records": matched,
            "primary_record": primary,
        }

    @staticmethod
    def _select_primary(matched_records: "List[Dict[str, Any]]") -> Dict[str, Any]:
        """
        Picks one candidate when more than one allele-matched rsID
        exists for the query variant -- genuinely possible for dbSNP
        (unlike ClinVar's one-accession-per-allele consolidation),
        e.g. a deprecated/merged rsID still returned alongside its
        current replacement before NCBI's merge propagates everywhere.
        Unlike `ClinVarClient._select_primary`, there is no
        `last_evaluated`-equivalent revision-date signal to break ties
        with here, so this deterministically prefers the lowest
        numeric rsID -- NCBI's merge convention keeps the older,
        lower-numbered rsID as canonical and retires newer duplicates
        into it, so this is the closest available proxy for "the
        current one," not an arbitrary pick.
        """

        def sort_key(record: Dict[str, Any]) -> int:
            digits = (record.get("rsid") or "rs0").lstrip("rs")
            return int(digits) if digits.isdigit() else 0

        return min(matched_records, key=sort_key)

    @staticmethod
    def _variant_match(entry: Dict[str, Any], variant: Variant) -> Optional[bool]:
        """
        Whether one dbSNP esummary entry's allele(s) match the queried
        variant, checked on both ref/alt and genomic position -- same
        principle as `database/clinvar_client.py::ClinVarClient.
        _variant_match`. The 'snp' db's `spdi` field lists every
        ALT allele the rsID covers as comma-joined SPDI strings (format
        `seq:0-based-pos:ref:alt`); an rsID matches if ANY of them
        agrees with the query's ref/alt at the query's position.

        Returns None (not False) when the entry carries no usable SPDI
        at all, so "checked, doesn't match" and "could not check" stay
        distinguishable -- same as `ClinVarMatchStatus`'s own reasoning.
        """
        spdi_field = entry.get("spdi")
        if not spdi_field:
            return None
        ref, alt = variant.ref.upper(), variant.alt.upper()
        checked_any = False
        for spdi in str(spdi_field).split(","):
            spdi = spdi.strip()
            if spdi.count(":") < 3:
                continue
            checked_any = True
            _, pos_str, spdi_ref, spdi_alt = spdi.split(":", 3)
            if spdi_ref.upper() != ref or spdi_alt.upper() != alt:
                continue
            try:
                if int(pos_str) + 1 == variant.pos:
                    return True
            except ValueError:
                continue
        return False if checked_any else None

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
            # Was 5 when only `uids[0]` was ever read; now every
            # candidate is fetched and allele-matched (see
            # `lookup_variant`), so a busy locus needs real headroom to
            # avoid truncating the correct rsID out of the candidate
            # set entirely -- matches `ClinVarClient._esearch`'s retmax.
            "retmax": 10,
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        payload = self._request_json(f"{self.eutils_base}/esearch.fcgi", params)
        return payload.get("esearchresult", {}).get("idlist", [])

    def _esummary(self, uids: List[str], variant: Variant) -> List[Dict[str, Any]]:
        """
        Summary (including the `spdi` allele field `_variant_match`
        needs) for every candidate UID a positional esearch returned --
        not just the one that used to be picked blindly. One batched
        call, same shape as `database/clinvar_client.py::ClinVarClient.
        _esummary`.
        """
        params = {
            "db": self.db,
            "id": ",".join(uids),
            "retmode": "json",
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        payload = self._request_json(f"{self.eutils_base}/esummary.fcgi", params)
        result = payload.get("result", {})
        records = []
        for uid in result.get("uids", []):
            entry = result.get(uid, {})
            records.append(
                {
                    "uid": uid,
                    "rsid": f"rs{uid}",
                    "spdi": entry.get("spdi"),
                    "clinical_significance": entry.get("clinical_significance"),
                    "genes": [g.get("name") for g in (entry.get("genes") or []) if isinstance(g, dict)],
                    "variant_match": self._variant_match(entry, variant),
                }
            )
        return records

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
            # dbSNP's own real, source-published version identifier --
            # the build this record was last updated against (verified
            # live, e.g. 157). dbSNP itself has no "database-wide
            # release version" beyond the build number; this is the
            # honest, real signal, captured per-record since that's
            # what this endpoint returns it against. See
            # `pipeline/provenance.py`'s docstring for the full audit.
            "dbsnp_build": payload.get("last_update_build_id"),
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
        if HEALTH.is_offline("dbSNP"):
            HEALTH.note_skip("dbSNP")
            raise ExternalAPIError(f"dbSNP request to '{url}' skipped: dbSNP was confirmed offline at startup.")

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
                result = lenient_json_loads(response.text, source=url)
                HEALTH.note_success("dbSNP")
                return result
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"dbSNP request attempt {attempt} failed: {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)
        HEALTH.note_failure("dbSNP")
        raise ExternalAPIError(f"dbSNP request to '{url}' failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}")
