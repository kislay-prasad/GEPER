"""
ClinVar client.

Queries NCBI ClinVar via the public E-utilities API (esearch +
esummary) for clinical significance annotations on a variant,
identified either by rsID or by a chrom/pos/ref/alt HGVS-style query.

Allele-aware record selection
------------------------------
`_esearch`'s query term is genomic-position-based (`{chrom}[chr] AND
{pos}[chrpos38/37]`), not allele-based -- ClinVar has no Entrez field
for "this exact REF/ALT". A single position frequently hosts *several*
distinct ClinVar-catalogued variants (a SNV, a different SNV, an
overlapping indel, ...), each its own accession with its own,
independent classification. Confirmed live: 17:43094298 (GRCh38)
returns three -- the queried `c.1233T>G` (Benign, expert panel), a
different substitution `c.1233T>C` (Conflicting), and an unrelated
2bp deletion `c.1232_1233del` (Pathogenic, expert panel) that merely
overlaps the same anchor position. Earlier versions of this client
took `records[0]` unconditionally, which -- since NCBI's esearch
relevance ordering is not guaranteed stable across queries -- could
(and, for the case above, did) attribute a completely different
variant's classification to the one actually being analyzed.

`query_variant` now classifies every returned record with
`variant_match: Optional[bool]` (see `_variant_match`) and reports one
of `ClinVarMatchStatus`'s three states, so "no record was found for
this exact variant" (`POSITION_ONLY` -- other, non-matching variants
exist here) is never conflated with "ClinVar has never heard of this
position" (`NOT_FOUND`), and neither is silently treated as "here's a
classification for this variant" the way a bare `records[0]` was.
Callers should read `primary_record`/`matched_records`, not `records`
(which stays the full, unfiltered, position-based list -- kept for
context/audit display, e.g. "other variants catalogued at this
position", never for attributing a classification).

Candidate-set completeness (the *retrieval* half of this, distinct
from matching)
------------------------------------------------------------------
The matcher above is only as good as the candidate set it gets to
search. `query_variant` accepts an optional `rsid` (resolved
upstream by `database/dbsnp_client.py::DbSNPClient.lookup_variant`,
which the orchestrator passes through when available) as a second,
more specific search route. That rsID resolution has the *exact same*
unfiltered-`[0]`-pick shape this module's own bug had:
`DbSNPClient.lookup_variant` takes dbSNP's positional esearch
`uids[0]` with no allele check, and a genomic position frequently has
more than one dbSNP UID (confirmed live: 17:43094298 returns
`['397508848', '80357024']` -- `rs397508848` is the 2bp deletion
`c.1232_1233del`, `rs80357024` is the actually-queried SNV
`c.1233T>G`; picking index 0 silently returns the deletion's rsID).
Passed to a ClinVar rsID-based search (`{rsid}[rs]`), a wrong rsID
doesn't just mis-rank results -- it returns a *narrower, wrong*
candidate set that never contains the correct record at all, so no
amount of correct allele-matching downstream can recover it (the
matcher has nothing to match). This was caught in production: 3 of 5
`test_data/nuclear_test.vcf` variants (every position where dbSNP had
more than one UID) resolved to `POSITION_ONLY` post-fix, purely
because the correct ClinVar record never reached `_variant_match`.

Fix: `query_variant` always runs the position-based esearch
(previously used only when no rsID was supplied) and, when an rsID is
also given, unions its esearch results into the same candidate set
rather than substituting for it. `DbSNPClient`'s own rsID resolution
is NOT fixed here -- it's a genuine, separate bug (anything else that
trusts its `uids[0]` rsID has the same exposure) but is out of this
fix's scope; not relying on it as the *only* ClinVar retrieval route
is what actually closes this gap, and does so without touching
`_variant_match`'s matching logic (which was already correct -- it
simply never got the right candidate to evaluate).
"""

import enum
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.ncbi_eutils import lenient_json_loads, parse_retry_after, warn_if_placeholder_contact

logger = get_logger(__name__)


class ClinVarMatchStatus(str, enum.Enum):
    """
    Three distinct outcomes of a ClinVar lookup -- deliberately not
    collapsed into a single boolean, for the same reason
    `pipeline/stage_schemas.py::StageStatus` and
    `pipeline/provenance.py::VersionStatus` each keep their own
    "checked, found nothing" state distinct from "didn't check"/
    "checked, found the wrong thing": conflating any of these is
    exactly the bug class this enum exists to prevent (see this
    module's docstring for the real BRCA1 case that motivated it).
    """

    NOT_FOUND = "not_found"        # esearch returned nothing at this position at all
    POSITION_ONLY = "position_only"  # record(s) exist at this position, but none match this allele
    MATCHED = "matched"            # at least one record's allele (and position) matches the query variant


class ClinVarClient:
    """Fetches clinical significance data for a variant from NCBI ClinVar."""

    def __init__(self):
        self.base_url = CONFIG.api.NCBI_EUTILS_BASE
        self.db = CONFIG.api.CLINVAR_DB
        warn_if_placeholder_contact(CONFIG.api.NCBI_EMAIL, caller="ClinVarClient")

    def query_variant(
        self, variant: Variant, rsid: Optional[str] = None, assembly: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Look up ClinVar records for a variant.

        Always runs the position-based search (previously only used
        when no rsID was available); when `rsid` is also given (from a
        prior dbSNP lookup), its results are UNIONED into the same
        candidate set rather than substituting for the positional
        search -- see this module's docstring for why: a position can
        have more than one dbSNP UID, `DbSNPClient.lookup_variant`
        resolves that ambiguity with the same kind of unchecked `[0]`
        pick this module used to have, and a wrong rsID's ClinVar
        search returns a candidate set that never contains the correct
        record at all. Deduped by ClinVar UID, so a variant with both
        a correct rsID and a reliable position never pays for two
        redundant esummary fetches of the same record.

        `assembly` is the genome build the input VCF's coordinates are
        on. ClinVar's Entrez position field is build-specific
        (`chrpos37` vs `chrpos38`); the bare `chrpos` field's default
        build is not something client code should rely on, so the
        build-qualified field is always used explicitly once the
        pipeline has resolved a build.

        Returns `found=True` (and a non-None `primary_record`) only
        when at least one returned record's own allele/position match
        the *queried* variant -- never merely because a search
        returned something (see this module's docstring for why that
        distinction matters). `records` is still the full, unfiltered,
        union candidate list, for callers that want to show other
        variants catalogued at this position as context.
        """
        position_term = self._positional_search_term(variant, assembly)
        queries_run = [position_term]
        uids = list(self._esearch(position_term))

        if rsid:
            rsid_term = f"{rsid}[rs]"
            queries_run.append(rsid_term)
            for uid in self._esearch(rsid_term):
                if uid not in uids:
                    uids.append(uid)

        if not uids:
            logger.info(f"No ClinVar records found for {' | '.join(queries_run)!r}.")
            return {
                "query": queries_run,
                "found": False,
                "match_status": ClinVarMatchStatus.NOT_FOUND.value,
                "record_count": 0,
                "matched_record_count": 0,
                "records": [],
                "matched_records": [],
                "primary_record": None,
            }

        records = self._esummary(uids, variant, assembly)
        matched_records = [r for r in records if r.get("variant_match") is True]

        if matched_records:
            match_status = ClinVarMatchStatus.MATCHED
            primary_record = self._select_primary(matched_records)
        else:
            match_status = ClinVarMatchStatus.POSITION_ONLY
            primary_record = None
            logger.info(
                f"ClinVar returned {len(records)} record(s) for {' | '.join(queries_run)!r} but none "
                f"match {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt} by allele -- "
                "reporting POSITION_ONLY, not attributing any of their classifications to this variant."
            )

        return {
            "query": queries_run,
            "found": match_status == ClinVarMatchStatus.MATCHED,
            "match_status": match_status.value,
            "record_count": len(records),
            "matched_record_count": len(matched_records),
            "records": records,
            "matched_records": matched_records,
            "primary_record": primary_record,
        }

    @staticmethod
    def _select_primary(matched_records: "List[Dict[str, Any]]") -> Dict[str, Any]:
        """
        Picks one record when more than one allele-matched record
        exists for the query variant. In practice this should be rare
        to never -- ClinVar consolidates one allele into one Variation
        ID/accession, so two independently-matching records for the
        exact same (position, ref, alt) would mean ClinVar itself has
        a duplicate/merged-record situation, not a genuine second
        variant. Handled defensively anyway: sorts by `last_evaluated`
        descending (a record's classification can be revised over
        time -- e.g. this exact BRCA1 case moved from an older,
        weaker classification to today's ENIGMA expert-panel "Benign"
        as of 2024-06-11 -- so the most recently evaluated record is
        the current clinical consensus, which is what a report should
        show). Records with an unparseable/missing date sort last
        rather than raising or silently winning a tie.
        """
        def sort_key(record: Dict[str, Any]) -> datetime:
            raw = record.get("last_evaluated")
            if not raw:
                return datetime.min
            try:
                return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M")
            except ValueError:
                return datetime.min

        return max(matched_records, key=sort_key)

    def _positional_search_term(self, variant: Variant, assembly: Optional[str]) -> str:
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
        # after the fact in `_esummary` (see `_variant_match`) so
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

    def _esummary(self, uids: list, variant: Optional[Variant] = None, assembly: Optional[str] = None) -> list:
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
                    "variant_match": self._variant_match(entry, variant, assembly),
                }
            )
        return records

    @staticmethod
    def _variant_match(
        entry: Dict[str, Any], variant: Optional[Variant], assembly: Optional[str]
    ) -> Optional[bool]:
        """
        Whether this ClinVar record's allele is actually the queried
        variant -- checked on BOTH ref/alt and genomic position, not
        ref/alt alone (an earlier version of this method, `_check_ref_alt`,
        only compared ref/alt; adding the position check closes the
        theoretical gap where two genuinely different variants near
        each other could coincidentally share the same ref/alt bases
        --defense in depth, since both values are already parsed out
        of the same field).

        Sourced from `variation_set[].canonical_spdi` (format
        `seq:0-based-pos:ref:alt` -- confirmed live: BRCA1's real
        canonical_spdi `NC_000017.11:43094297:A:C` is this variant's
        own 1-based VCF position 43094298 minus one, i.e. the same
        locus). `variation_loc[].start` (assembly-qualified, 1-based)
        is checked as a second, independent position source when
        present, since `variation_set` entries have been observed with
        `canonical_spdi` but empty `ref`/`alt` fields directly on
        `variation_loc` -- SPDI is what actually carries the alleles.

        KNOWN LIMITATION, disclosed rather than silently assumed away:
        for indels, ClinVar's SPDI normalization can legitimately
        represent the identical variant with different ref/alt
        strings than GEPER's own (VCF-anchored) representation (e.g. a
        different shared-anchor-base convention). This check does not
        attempt indel re-normalization, so a genuine indel match can
        come back `False` rather than `True` in that case. The safe
        default either way is to NOT attribute an unconfirmed record's
        classification to the query variant, so this asymmetry errs
        toward under- rather than over-attribution -- consistent with
        every other "never fabricate evidence" guarantee in this
        codebase. SNV matching (the case that motivated this fix) is
        unaffected: a single base has no alternate normalization.

        Returns None (not False) when no `variation_set` entry carries
        a usable SPDI at all, so "checked, doesn't match" and "could
        not check" stay distinguishable to callers -- same principle
        as `ClinVarMatchStatus` itself.
        """
        if variant is None:
            return None
        variation_set = entry.get("variation_set")
        if not isinstance(variation_set, list):
            return None

        build_name = (
            "GRCh37"
            if assembly and assembly.strip().upper().replace("-", "").replace("_", "") in {"GRCH37", "HG19", "B37"}
            else "GRCh38"
        )
        ref, alt = variant.ref.upper(), variant.alt.upper()
        checked_any = False

        for v in variation_set:
            if not isinstance(v, dict):
                continue
            spdi = v.get("canonical_spdi")
            if not spdi or spdi.count(":") < 3:
                continue
            checked_any = True
            _, spdi_pos, spdi_ref, spdi_alt = spdi.split(":", 3)
            if spdi_ref.upper() != ref or spdi_alt.upper() != alt:
                continue

            # Alleles agree -- confirm position too. SPDI's own
            # position is 0-based; +1 makes it comparable to the VCF's
            # 1-based `variant.pos`.
            try:
                if int(spdi_pos) + 1 == variant.pos:
                    return True
            except ValueError:
                pass
            # Fall back to the assembly-matched variation_loc entry's
            # own (1-based) start, in case the SPDI position couldn't
            # be parsed cleanly.
            for loc in v.get("variation_loc") or []:
                if not isinstance(loc, dict):
                    continue
                if (loc.get("assembly_name") or "").strip() != build_name:
                    continue
                start = loc.get("start")
                if start and str(start).strip().isdigit() and int(start) == variant.pos:
                    return True

        return False if checked_any else None

    def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        GET `url` and return the parsed JSON body, retrying up to
        `CONFIG.api.MAX_RETRIES` times.

        A 429 (NCBI's rate-limit response) is handled specially: NCBI
        sends a `Retry-After` header telling the client exactly how
        long to wait (confirmed live 2026-07-31: `Retry-After: 2` on a
        real 429), so that's honored instead of guessing via the
        default exponential backoff, which is what every other failure
        mode still uses. A 429 still consumes one of the retry
        attempts -- with `CONFIG.api.NCBI_API_KEY` set (10 req/s vs. 3
        req/s unauthenticated), this should now rarely happen at all,
        but polite handling matters either way.

        The JSON body itself is parsed leniently (`lenient_json_loads`)
        rather than via `response.json()`'s strict parser -- a real
        ClinVar response has been observed to contain a raw control
        character inside a JSON string, which strict parsing rejects
        as a `ValueError` and (before this fix) this loop then retried
        as if it were a network failure, exhausting all attempts on a
        response that was actually complete, valid data. See
        `utils/ncbi_eutils.py::lenient_json_loads`'s docstring for why
        this is a data-layer quirk to route around, not a failure to
        retry into oblivion.
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
                        f"ClinVar request attempt {attempt} rate-limited (429); waiting {wait:.1f}s "
                        f"({'NCBI Retry-After header' if retry_after is not None else 'default backoff'})."
                    )
                    if attempt < CONFIG.api.MAX_RETRIES:
                        time.sleep(wait)
                    continue
                response.raise_for_status()
                return lenient_json_loads(response.text, source=url)
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"ClinVar request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(
            f"ClinVar request to '{url}' failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}"
        )
