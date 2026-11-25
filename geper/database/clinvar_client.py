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

Record types (ClinVar audit C2)
-------------------------------
A ClinVar record is not always one variant. A haplotype, a diplotype or a
compound heterozygote is ONE record whose `variation_set` lists every
component, and whose classification belongs to the combination. Allele
matching alone therefore attached a haplotype's "Likely pathogenic" (or a
diplotype's "drug response, practice guideline") to each component variant.
Each record now carries `record_type` (ClinVar's `obj_type`),
`component_count` and a `record_role`; only an ELIGIBLE record -- a recognised
single-allele type, exactly one component, allele-matched, with an
individual-variant classification -- can populate `matched_records` and
`primary_record`. Every other record that contains the allele is kept in
`context_records`, labelled and never used as evidence. The policy and its
official sources are in `utils/clinvar_records.py` and
`geper/CLINVAR_RECORD_TYPES.md`.

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
from pipeline.variant_normalization import bare_spdi
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.clinvar_records import (
    EVIDENCE_POLICY_VERSION,
    as_text,
    classifications_conflict,
    classify_record_role,
    is_eligible,
    order_records,
    record_type_warning,
    role_label,
    summarize_context,
)
from utils.logger import get_logger
from utils.ncbi_eutils import (
    ESEARCH_MAX_RECORDS,
    ESUMMARY_BATCH_SIZE,
    complete_esearch_idlist,
    lenient_json_loads,
    parse_retry_after,
    parse_vcv_submitters,
    warn_if_placeholder_contact,
)
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)


def _entrez_chrom(chrom: str) -> str:
    """
    Strip an optional 'chr' prefix for NCBI Entrez's ClinVar `[chr]`
    field -- which, for the mitochondrial contig specifically, wants
    'MT', never 'M'. Confirmed live (round 15): `MT[chr]` returns
    hits, `M[chr]` returns zero with an NCBI "phrase not found"
    warning. A bare strip-and-pass-through silently drops every MT
    variant whose VCF spelled the contig 'M'/'chrM' (same M-family
    special case already handled in pipeline.hgvs_utils._strip_chr and
    models.alphamissense._normalize_chrom_for_catalogue).
    """
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return "MT" if bare.upper() in ("M", "MT") else bare


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

    NOT_FOUND = "not_found"  # esearch returned nothing at this position at all
    POSITION_ONLY = "position_only"  # record(s) exist at this position, but none match this allele
    MATCHED = "matched"  # at least one record's allele (and position) matches the query variant

    def __bool__(self):
        raise TypeError(
            "ClinVarMatchStatus has no truth value -- every member of a `str` enum is truthy, "
            "so `if x:`/`if not x:` silently collapses NOT_FOUND, POSITION_ONLY and MATCHED "
            "into one answer, which is exactly the distinction this type exists to "
            "keep. Compare explicitly, e.g. `x is ClinVarMatchStatus.MATCHED`."
        )


def self_build_offset(variation_loc: Any, build_name: str) -> Optional[int]:
    """
    GRCh38 start minus `build_name` start for ONE ClinVar record, read from
    that record's own `variation_loc` list -- or None when either start is
    missing or not an integer. Used only to place the record's GRCh38
    `canonical_spdi` into the query's build for comparison.
    """
    starts: Dict[str, int] = {}
    for loc in variation_loc or []:
        if not isinstance(loc, dict):
            continue
        name = (loc.get("assembly_name") or "").strip()
        start = str(loc.get("start") or "").strip()
        if name in ("GRCh38", build_name) and start.isdigit() and name not in starts:
            starts[name] = int(start)
    if "GRCh38" not in starts or build_name not in starts:
        return None
    return starts["GRCh38"] - starts[build_name]


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
                "evidence_policy": EVIDENCE_POLICY_VERSION,
                "record_count": 0,
                "matched_record_count": 0,
                "context_record_count": 0,
                "records": [],
                "matched_records": [],
                "context_records": [],
                "individual_records_conflict": False,
                "multiple_eligible_records": False,
                "record_type_warnings": [],
                "primary_record": None,
            }

        records = self._esummary(uids, variant, assembly)
        # C2: only an ELIGIBLE record (recognised single-allele type, one component, allele
        # matched, individual-variant classification) is individual-variant evidence. Every other
        # record that contains the allele is kept as labelled, context-only information.
        # See `utils/clinvar_records.py`.
        matched_records = order_records(r for r in records if r.get("eligible_for_variant_classification"))
        context_records = order_records(
            r for r in records if r.get("variant_match") is True and not r.get("eligible_for_variant_classification")
        )
        # Several eligible records with DIFFERENT classifications are preserved, never resolved by
        # picking the newest: no primary record is chosen then.
        individual_records_conflict = classifications_conflict(matched_records)

        if matched_records:
            match_status = ClinVarMatchStatus.MATCHED
            primary_record = None if individual_records_conflict else matched_records[0]
            # Round 30: submitting organisation(s) for every record that
            # can actually contribute to a criterion (PS1/PM5/BP6 all
            # read `matched_records`/`primary_record`, never `records`'
            # position-only noise) -- see `_fetch_submitters` for why
            # this needs a second request. Best-effort: a failure here
            # (logged inside `_fetch_submitters`) leaves `submitters`
            # unset rather than blocking the classification-relevant
            # result already computed above.
            # Submitters are fetched for the eligible records only (a pharmacogene position can
            # carry dozens of context records that never reach PS1/PM5/BP6).
            submitters_by_uid = self._fetch_submitters([r["uid"] for r in matched_records])
            for r in matched_records:
                r["submitters"] = submitters_by_uid.get(r["uid"])
        else:
            match_status = ClinVarMatchStatus.POSITION_ONLY
            primary_record = None
            logger.info(
                f"ClinVar returned {len(records)} record(s) for {' | '.join(queries_run)!r} but none is an "
                f"individual-variant record with a classification for {variant.chrom}:{variant.pos} "
                f"{variant.ref}>{variant.alt} ({len(context_records)} context-only record(s) contain the allele) "
                "-- reporting POSITION_ONLY, not attributing any of their classifications to this variant."
            )
        if context_records:
            logger.info(
                f"ClinVar: {len(context_records)} context-only record(s) contain {variant.chrom}:{variant.pos} "
                f"{variant.ref}>{variant.alt} and are not used as evidence: {summarize_context(context_records)}."
            )
        # A record that contains the allele but has a missing/unknown record type (or an impossible
        # component count) is context only (fail-safe); the structured warning names its accession and
        # type so a new NCBI record type is noticed rather than silently demoting evidence. Never fatal.
        record_type_warnings = [w for w in (record_type_warning(r) for r in records) if w is not None]
        for warning in record_type_warnings:
            logger.warning(
                f"ClinVar record {warning['accession'] or warning['uid']} contains {variant.chrom}:{variant.pos} "
                f"{variant.ref}>{variant.alt} but its record type is {warning['reason']} "
                f"(obj_type={warning['record_type']!r}, components={warning['component_count']}); "
                "treated as context only."
            )

        return {
            "query": queries_run,
            "found": match_status == ClinVarMatchStatus.MATCHED,
            "match_status": match_status.value,
            "evidence_policy": EVIDENCE_POLICY_VERSION,
            "record_count": len(records),
            "matched_record_count": len(matched_records),
            "context_record_count": len(context_records),
            "records": records,
            "matched_records": matched_records,
            "context_records": context_records,
            "individual_records_conflict": individual_records_conflict,
            "multiple_eligible_records": len(matched_records) > 1,
            "record_type_warnings": record_type_warnings,
            "primary_record": primary_record,
        }

    @staticmethod
    def _select_primary(matched_records: "List[Dict[str, Any]]") -> Dict[str, Any]:
        """
        RETAINED FOR COMPATIBILITY; `query_variant` no longer uses it (C2). It picks the most
        recently evaluated record, which is not safe as evidence selection: the newest record can
        be a haplotype, or can disagree with an older eligible record. `query_variant` now orders
        eligible records deterministically (`utils.clinvar_records.order_records`) and chooses no
        primary at all when eligible records conflict.

        Original description: picks one record when more than one allele-matched record
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
        chrom = _entrez_chrom(variant.chrom)
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
        #
        # Indels widen this to a 3-position range (pos-1:pos+1), not a
        # single point. Confirmed live: BRCA1 c.1232_1233del
        # (VCV000054169)'s own `chrpos38` indexes it at 43094298 --
        # one MORE than the VCF anchor position 43094297 -- while VHL
        # c.422dup (VCV000411979) indexes at 10146594, exactly the
        # anchor position, no offset at all. ClinVar's own position
        # field is evidently not indexed by a single fixed rule
        # relative to the VCF anchor for indels (deletion vs. insertion
        # land differently), so a single-point query can miss the
        # correct record entirely -- a retrieval gap, not an allele-
        # matching one (`_variant_match` still gates actual
        # attribution). SNVs keep the tighter single-point query, since
        # there's no anchor-base offset to account for.
        if len(variant.ref) != len(variant.alt) and variant.pos > 1:
            return f"{chrom}[chr] AND {variant.pos - 1}:{variant.pos + 1}[{position_field}]"
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
            # Was 10, read without checking `count`: the +/-1 bp indel window
            # at BRCA1 c.68_69del holds 20 records and the matching one
            # (VCV000017662) was never fetched. Now the complete set is
            # requested and verified -- see `complete_esearch_idlist`.
            "retmax": ESEARCH_MAX_RECORDS,
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY

        payload = self._request_json(f"{self.base_url}/esearch.fcgi", params)
        return complete_esearch_idlist(payload, source="ClinVarClient")

    def _esummary(self, uids: list, variant: Optional[Variant] = None, assembly: Optional[str] = None) -> list:
        # Batched: `_esearch` can now return up to ESEARCH_MAX_RECORDS IDs,
        # more than one GET request's `id=` list should carry.
        records: List[Dict[str, Any]] = []
        for start in range(0, len(uids), ESUMMARY_BATCH_SIZE):
            records.extend(self._esummary_batch(uids[start : start + ESUMMARY_BATCH_SIZE], variant, assembly))
        return records

    def _esummary_batch(
        self, uids: List[str], variant: Optional[Variant], assembly: Optional[str]
    ) -> List[Dict[str, Any]]:
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
            # C2 hardening: the record-type policy only ever sees text. A malformed (non-text,
            # unhashable) value from NCBI is treated as missing -- context only -- never raised.
            raw_record_type = entry.get("obj_type")
            record_type = as_text(raw_record_type)
            description = as_text(germline.get("description"))
            review_status = as_text(germline.get("review_status"))
            variation_set = entry.get("variation_set")
            component_count = len(variation_set) if isinstance(variation_set, list) else None
            role = classify_record_role(record_type, component_count, description)
            variant_match = self._variant_match(entry, variant, assembly)
            records.append(
                {
                    "uid": uid,
                    "title": entry.get("title"),
                    "clinical_significance": description,
                    "review_status": review_status,
                    "last_evaluated": germline.get("last_evaluated"),
                    "condition": [
                        trait.get("trait_name") for trait in germline.get("trait_set", []) if isinstance(trait, dict)
                    ],
                    "accession": entry.get("accession"),
                    # `variant_match` keeps its meaning: this record CONTAINS the queried allele.
                    # Whether it is individual-variant evidence is `eligible_for_variant_classification`.
                    "variant_match": variant_match,
                    "record_type": record_type,
                    # Present only when NCBI sent an `obj_type` that is not text (kept out of the stored result).
                    **({"record_type_malformed": True} if raw_record_type is not None and record_type is None else {}),
                    "component_count": component_count,
                    "record_role": role,
                    "record_role_label": role_label(role, record_type),
                    "eligible_for_variant_classification": is_eligible(role, variant_match),
                }
            )
        return records

    def _fetch_submitters(self, uids: List[str]) -> Dict[str, List[Dict[str, Optional[str]]]]:
        """
        Submitting organisation(s) for each ClinVar `uid`, via a single
        batched `efetch&rettype=vcv` request (VCV XML) -- NOT available
        from the `esummary` JSON `_esummary` already parses (confirmed
        live: `supporting_submissions.scv` there is bare SCV
        accessions with no org name; `parse_vcv_submitters`'s docstring
        has the full comparison). Batched the same way `_esummary`
        batches its own `id` parameter, so this is one extra request
        per `query_variant` call, not one per record.

        Best-effort and never raises: a submitter lookup is
        supplementary provenance, not something PS1/PM5/BP6's own
        pathogenic/benign classification depends on, so a network or
        parse failure here is logged and yields `{}` (leaving
        `submitters` unset on every affected record) rather than
        surfacing as an `ExternalAPIError` that would block the
        classification-relevant result `query_variant` already
        computed before calling this.
        """
        if not uids:
            return {}
        params = {
            "db": self.db,
            "id": ",".join(uids),
            "rettype": "vcv",
            "is_variationid": "true",
            "tool": CONFIG.api.NCBI_TOOL_NAME,
            "email": CONFIG.api.NCBI_EMAIL,
        }
        if CONFIG.api.NCBI_API_KEY:
            params["api_key"] = CONFIG.api.NCBI_API_KEY
        try:
            response = requests.get(
                f"{self.base_url}/efetch.fcgi", params=params, timeout=CONFIG.api.REQUEST_TIMEOUT_SECS
            )
            response.raise_for_status()
            return parse_vcv_submitters(response.text)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad; see docstring above.
            logger.warning(f"ClinVar submitter lookup failed for {len(uids)} record(s); submitters left unset: {exc}")
            return {}

    @staticmethod
    def _variant_match(entry: Dict[str, Any], variant: Optional[Variant], assembly: Optional[str]) -> Optional[bool]:
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

        Indel re-normalization: a `canonical_spdi` is not guaranteed to
        use the same anchoring convention as GEPER's own (VCF-style,
        single-base-anchored) query `variant`. Two confirmed-live
        mismatch shapes, both closed by comparing both sides' *bare*
        SPDI form (`pipeline/variant_normalization.bare_spdi` --
        prefix/suffix trim with no anchor-base floor, so ref/alt may
        end up fully empty; comparison-only, never used to emit a VCF
        record):
          1. VHL c.422dup (VCV000411979): canonical_spdi
             `NC_000003.12:10146593:AA:AAA` is a non-minimal 2-base
             window sharing a leading 'A'; the query variant normalizes
             to `10146594 A>AA`. Raw-string comparison never matches;
             both reduce to the same bare form `(10146594, '', 'A')`.
          2. BRCA1 c.1232_1233del (VCV000054169): canonical_spdi
             `NC_000017.11:43094297:AT:` is already fully bare (empty
             inserted sequence), while the query variant's VCF-style
             anchored form is `43094297 CAT>C` -- a plain parsimony
             trim (which must keep >=1 anchor base) can't reduce `C`
             any further, so it alone is not enough; both reduce to the
             same bare form `(43094298, 'AT', '')`.
        This is re-normalization to a shared comparison frame, not
        loosened matching: a mismatch after both sides are
        independently reduced to their own bare form is still a real
        mismatch. No `fetch_base`/reference sequence is needed for
        either case -- both are reference-free prefix/suffix trimming,
        not repeat-region re-alignment.

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
        query_bare_pos, query_bare_ref, query_bare_alt = bare_spdi(variant.pos, ref, alt)

        for v in variation_set:
            if not isinstance(v, dict):
                continue
            spdi = v.get("canonical_spdi")
            if not spdi or spdi.count(":") < 3:
                continue
            checked_any = True
            _, spdi_pos, spdi_ref, spdi_alt = spdi.split(":", 3)

            # SPDI's own position is 0-based; +1 makes it comparable to
            # the VCF's 1-based `variant.pos` before bare-trimming
            # (trimming a common *prefix* shifts position, so this must
            # happen before, not after, the +1).
            try:
                spdi_pos_1based = int(spdi_pos) + 1
            except ValueError:
                spdi_pos_1based = None

            if spdi_pos_1based is not None:
                cand_pos, cand_ref, cand_alt = bare_spdi(spdi_pos_1based, spdi_ref.upper(), spdi_alt.upper())
                if (cand_pos, cand_ref, cand_alt) == (query_bare_pos, query_bare_ref, query_bare_alt):
                    return True
                # GRCh37 queries (Priority 2 ClinVar remediation). ClinVar's
                # canonical_spdi is on GRCh38 (e.g. NC_000017.11), so for a
                # GRCh37 variant the comparison above is in the wrong frame,
                # and the raw-allele fallback below only works for SNVs (an
                # indel's SPDI and VCF anchoring differ). Live-confirmed: BRCA1
                # c.68_69del VCV000017662 never matched GRCh37 17:41276044
                # ACT>A. Translate the SPDI position into the query's build
                # using THIS record's own GRCh38 and query-build
                # `variation_loc` starts (the same locus in both builds, so
                # their difference is the local offset), then compare bare
                # forms as above. No external liftover; if either start is
                # missing, nothing is translated and nothing new can match.
                if build_name != "GRCh38":
                    offset = self_build_offset(v.get("variation_loc"), build_name)
                    if offset is not None:
                        cand_pos, cand_ref, cand_alt = bare_spdi(
                            spdi_pos_1based - offset, spdi_ref.upper(), spdi_alt.upper()
                        )
                        if (cand_pos, cand_ref, cand_alt) == (query_bare_pos, query_bare_ref, query_bare_alt):
                            return True

            if spdi_ref.upper() != ref or spdi_alt.upper() != alt:
                continue

            # Untrimmed alleles already agree (the common SNV case) --
            # confirm position too, same as above but without trimming.
            if spdi_pos_1based is not None and spdi_pos_1based == variant.pos:
                return True
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
        if HEALTH.is_offline("ClinVar"):
            HEALTH.note_skip("ClinVar")
            logger.warning(f"ClinVar request to '{url}' skipped: ClinVar was confirmed offline at startup.")
            # Round 26: the message actually raised (folded verbatim into
            # `clinvar_error` -- see `report/clinical_report_builder.py::
            # _clinical_evidence` -- and rendered into every report
            # unconditionally by `report/report_generator.py`, plus
            # embedded verbatim in geper_results.json's `clinvar` key by
            # `report/json_builder.py`) must not embed `url`; full detail
            # goes to the log line above only. Same pattern rounds 20-24
            # applied to SpliceBERT/MMSplice/UniProt/InterPro/ClinGen/
            # AlphaFold DB.
            raise ExternalAPIError("ClinVar request skipped: ClinVar was confirmed offline at startup.")

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
                result = lenient_json_loads(response.text, source=url)
                HEALTH.note_success("ClinVar")
                return result
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"ClinVar request attempt {attempt} failed: {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)
        HEALTH.note_failure("ClinVar")
        logger.warning(
            f"ClinVar request to '{url}' failed after {CONFIG.api.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 26: see the sanitization note on the offline-skip raise
        # above -- the raised message must not embed `url`/`last_error`.
        raise ExternalAPIError(f"ClinVar request failed after {CONFIG.api.MAX_RETRIES} attempts")
