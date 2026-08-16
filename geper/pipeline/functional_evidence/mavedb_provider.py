"""
MaveDB provider -- secondary PS3/BS3 source, consulted only for a
variant the ClinGen Evidence Repository has no curation for (see
`lookup.py`).

MaveDB (mavedb.org) distributes raw multiplexed-assay-of-variant-effect
(MAVE) scores -- saturation genome editing, deep mutational scans,
reporter assays -- as a numeric score per variant, plus (for a growing
share of score sets) investigator-provided `scoreCalibrations`: named
functional-classification buckets ("Functional" / "Intermediate" /
"Non-functional", mapped to "normal" / "not_specified" / "abnormal")
with explicit score-range thresholds and a cited threshold source
publication. This provider only ever buckets a raw score against a
score set's *own* calibration -- it never invents a threshold. A score
set with no calibration at all cannot produce a PS3/BS3 call and
contributes nothing (not a fabricated guess).

License: MaveDB relicensed nearly all of its corpus from the earlier
non-commercial CC-BY-NC-SA to a permissive default, but licensing is
set *per score set* by its own submitter, not platform-wide -- a
stray CC-BY-NC-SA (or other non-commercial) score set can still exist
(see `DATA_SOURCE_LICENSE_AUDIT.md`'s "residual, code-unenforced risk"
finding, 2026-08-08). `_index_one_score_set` below therefore checks
every score set's own `license.shortName` field (confirmed against
MaveDB's own `/api/v1/licenses/` endpoint, live, 2026-08-08 -- the
authoritative vocabulary, not guessed) before using its data as
evidence, the same "verify before trust, at the point of use" principle
`pipeline/models/borzoi_plugin.py`'s `BorzoiLicenseGuardError` already
applies to Borzoi's weight source -- adapted here to a per-record soft
skip rather than a hard plugin-load failure, since one score set's
license has no bearing on any other score set's, or any other gene's
(see `fetch_gene_index`'s own "one bad score set must not lose every
other candidate" reasoning, which this reuses). A score set whose
license is missing or not in `_COMMERCIAL_SAFE_LICENSE_SHORT_NAMES`
contributes nothing, logged, exactly like a score set with no usable
calibration -- never silently used anyway. This replaces the earlier,
purely manual "spot-checked live for BRCA1/TP53 during development"
verification with a check applied automatically to every query.
CFTR returned zero score sets -- a genuine coverage gap, confirmed live
(`POST /score-sets/search {"text": "CFTR"}` -> `{"scoreSets": [],
"numScoreSets": 0}`), the same gap ERepo has.

Performance: a gene like BRCA1 can have 60+ score sets (many are
per-exon replicate splits of the same underlying assay). Fetching
every one's variant-data CSV per gene would be a lot of network calls
for a secondary, best-effort source, so this provider sorts candidates
by `numVariants` descending and caps at
`CONFIG.functional_evidence.MAVEDB_MAX_SCORE_SETS_PER_GENE` -- a
disclosed coverage/performance tradeoff, not silent truncation. Exactly
like ERepo, this is one (multi-call) fetch per *gene*, cached and
reused for every subsequent variant in that gene.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.functional_evidence.models import FunctionalEvidenceRecord
from pipeline.functional_evidence.utils import normalize_hgvs_c
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger
from utils.service_health import HEALTH, is_transient_http_error

logger = get_logger(__name__)

_CLASSIFICATION_TO_CALL = {"abnormal": "PS3", "normal": "BS3"}  # "not_specified" contributes no call

# MaveDB's own license vocabulary (confirmed live against
# `GET /api/v1/licenses/`, 2026-08-08 -- five records, `shortName` is
# the field a score set's own `license` object carries):
#   "CC0"                        -- active, public domain -- SAFE
#   "CC BY 4.0"                  -- active, attribution only -- SAFE
#   "CC BY-SA 4.0"               -- active, attribution + share-alike -- SAFE
#     (share-alike restricts *redistributing modified copies* under a
#     different license; it does not restrict commercial use itself,
#     and GEPER only ever reads a score/classification as evidence,
#     never redistributes MaveDB's underlying dataset -- so this is
#     commercial-use-safe for GEPER's purpose.)
#   "CC BY-NC-SA 4.0"            -- inactive/deprecated, NON-COMMERCIAL -- UNSAFE
#   "Other - See Data Usage Guidelines" -- inactive, terms unspecified -- UNSAFE
# Only the three explicitly-confirmed-permissive names are listed here
# -- anything else (a value not in this set, a missing `license`
# field entirely, or a future license MaveDB adds that hasn't been
# reviewed) fails closed as unsafe, matching GEPER's established
# tri-state "don't guess a favorable default" convention elsewhere
# (e.g. `commercial_use_allowed: Optional[bool]` in
# `pipeline/models/base.py::ModelMetadata`, which treats `None` the
# same way -- never silently treated as permitted).
_COMMERCIAL_SAFE_LICENSE_SHORT_NAMES = frozenset({"CC0", "CC BY 4.0", "CC BY-SA 4.0"})


class MaveDBFunctionalEvidenceProvider:
    """Queries MaveDB for a gene's calibrated functional-assay classifications."""

    name = "mavedb"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = (endpoint or CONFIG.functional_evidence.MAVEDB_API_ENDPOINT).rstrip("/")

    def is_available(self) -> bool:
        return bool(CONFIG.functional_evidence.MAVEDB_ENABLED) and not CONFIG.functional_evidence.OFFLINE_MODE

    def fetch_gene_index(self, gene_symbol: str) -> Dict[str, FunctionalEvidenceRecord]:
        """
        Returns a dict mapping a version-stripped HGVS.c string (see
        `utils.normalize_hgvs_c`) to the single best
        `FunctionalEvidenceRecord` MaveDB's calibrated score sets
        produce for that variant. Raises `ExternalAPIError` on a
        genuine request failure for the search step itself; a failure
        fetching one individual score set's data is logged and that
        score set is simply skipped (a partial MaveDB index is still
        useful, unlike a failed search which means "no candidates at
        all").
        """
        score_sets = self._search_score_sets(gene_symbol)
        candidates = self._filter_and_rank(score_sets, gene_symbol)
        index: Dict[str, FunctionalEvidenceRecord] = {}
        for score_set in candidates[: CONFIG.functional_evidence.MAVEDB_MAX_SCORE_SETS_PER_GENE]:
            urn = score_set.get("urn")
            try:
                self._index_one_score_set(urn, index)
            except Exception as exc:  # noqa: BLE001 - one bad score set must not lose every other candidate
                logger.warning(f"MaveDB score set '{urn}' could not be indexed for gene '{gene_symbol}': {exc}")
        return index

    # -- gene-level search + candidate ranking -------------------------

    def _search_score_sets(self, gene_symbol: str) -> List[Dict[str, Any]]:
        url = f"{self.endpoint}/score-sets/search"

        if HEALTH.is_offline("MaveDB"):
            HEALTH.note_skip("MaveDB")
            logger.warning(f"MaveDB search request to '{url}' skipped: MaveDB was confirmed offline at startup.")
            # Round 28: the message actually raised propagates out of
            # `fetch_gene_index` (this method is NOT wrapped in the
            # per-score-set swallowing try/except `_index_one_score_set`
            # uses -- see this class's own docstring) to
            # `pipeline/functional_evidence/lookup.py::_match_mavedb`,
            # which folds it into `errors`/the returned `error` field,
            # embedded verbatim in geper_results.json AND rendered
            # unconditionally into every Markdown report's "Stage
            # Warnings / Errors" section via `pipeline/orchestrator.py::
            # _run_functional_evidence_stage`. Must not embed `url`; full
            # detail goes to the log line above only.
            raise ExternalAPIError("MaveDB search request skipped: MaveDB was confirmed offline at startup.")

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.functional_evidence.MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url,
                    json={"text": gene_symbol},
                    timeout=CONFIG.functional_evidence.QUERY_TIMEOUT_SECS,
                )
                response.raise_for_status()
                result = response.json().get("scoreSets", []) or []
                HEALTH.note_success("MaveDB")
                return result
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"MaveDB search attempt {attempt} failed for gene '{gene_symbol}': {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.functional_evidence.MAX_RETRIES:
                    time.sleep(CONFIG.functional_evidence.RETRY_BACKOFF_SECS * attempt)
        HEALTH.note_failure("MaveDB")
        logger.warning(
            f"MaveDB search request to '{url}' failed after "
            f"{CONFIG.functional_evidence.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: see the sanitization note on the offline-skip raise
        # above -- same reach, same fix.
        raise ExternalAPIError(f"MaveDB search request failed after {CONFIG.functional_evidence.MAX_RETRIES} attempts")

    @staticmethod
    def _filter_and_rank(score_sets: List[Dict[str, Any]], gene_symbol: str) -> List[Dict[str, Any]]:
        gene_upper = gene_symbol.strip().upper()

        def _targets_gene(score_set: Dict[str, Any]) -> bool:
            for target in score_set.get("targetGenes") or []:
                name = (target.get("name") or "").upper()
                mapped = (target.get("mappedHgncName") or "").upper()
                if mapped == gene_upper:
                    return True
                # A target name like "BRCA1 RING domain" or "BRCA1
                # translation start through RING domain" still refers
                # to this gene -- match on the gene symbol as a whole
                # word rather than requiring an exact target-name match.
                if any(word.strip(",.") == gene_upper for word in name.split()):
                    return True
            return False

        matches = [s for s in score_sets if _targets_gene(s)]
        matches.sort(key=lambda s: s.get("numVariants") or 0, reverse=True)
        return matches

    # -- per-score-set indexing -----------------------------------------

    def _index_one_score_set(self, urn: str, index: Dict[str, FunctionalEvidenceRecord]) -> None:
        metadata = self._get_json(f"{self.endpoint}/score-sets/{urn}")

        license_short_name = (metadata.get("license") or {}).get("shortName")
        if license_short_name not in _COMMERCIAL_SAFE_LICENSE_SHORT_NAMES:
            logger.warning(
                f"MaveDB score set '{urn}' has license '{license_short_name!r}', which is not in "
                f"GEPER's confirmed commercial-use-safe set {sorted(_COMMERCIAL_SAFE_LICENSE_SHORT_NAMES)} "
                "-- excluding it from PS3/BS3 evidence (see DATA_SOURCE_LICENSE_AUDIT.md)."
            )
            return  # not a confirmed commercial-safe license -- nothing usable from this score set

        calibration = _best_calibration(metadata.get("scoreCalibrations"))
        if calibration is None:
            return  # no calibrated classification available -- nothing usable from this score set

        publication = _first_publication(metadata)
        research_use_only = bool(calibration.get("researchUseOnly"))
        strength = (
            CONFIG.functional_evidence.MAVEDB_RESEARCH_USE_ONLY_STRENGTH
            if research_use_only
            else CONFIG.functional_evidence.MAVEDB_CLINICAL_GRADE_STRENGTH
        )

        rows = self._get_variant_rows(urn)
        for row in rows:
            hgvs_nt = row.get("hgvs_nt")
            score = _safe_float(row.get("scores.score") or row.get("score"))
            if not hgvs_nt or hgvs_nt == "NA" or score is None:
                continue
            classification = _bucket_score(score, calibration)
            call = _CLASSIFICATION_TO_CALL.get(classification)
            if call is None:
                continue  # "not_specified" (intermediate) -- real result, just not PS3/BS3 evidence
            key = normalize_hgvs_c(hgvs_nt)
            if key is None:
                continue
            # First score set wins for a given variant (candidates are
            # already ranked largest/most-complete-assay first); this
            # keeps the index simple rather than trying to adjudicate
            # between two independent MAVE assays of the same variant.
            if key in index:
                continue
            index[key] = FunctionalEvidenceRecord(
                source="mavedb",
                call=call,
                strength=strength,
                matched_hgvs="",  # filled in by lookup.py at match time
                classification_outcome=None,
                raw_score=score,
                score_set_urn=urn,
                functional_classification=classification,
                research_use_only=research_use_only,
                publication=publication,
            )

    def _get_variant_rows(self, urn: str) -> List[Dict[str, str]]:
        url = f"{self.endpoint}/score-sets/{urn}/variants/data"
        text = self._get_text(url)
        return list(csv.DictReader(io.StringIO(text)))

    def _get_json(self, url: str) -> Dict[str, Any]:
        response = self._get(url)
        return response.json()

    def _get_text(self, url: str) -> str:
        response = self._get(url)
        return response.text

    def _get(self, url: str) -> "requests.Response":
        if HEALTH.is_offline("MaveDB"):
            HEALTH.note_skip("MaveDB")
            logger.warning(f"MaveDB request to '{url}' skipped: MaveDB was confirmed offline at startup.")
            # Round 28: currently, every caller of `_get`/`_get_json`/
            # `_get_text` (`_index_one_score_set`, `_get_variant_rows`)
            # is wrapped in `fetch_gene_index`'s own per-score-set
            # `except Exception: logger.warning(...)` (see this class's
            # docstring), so this specific raise is log-only today --
            # but sanitized anyway, matching `_search_score_sets`'s
            # fix and every other site in this leak class, so a future
            # caller added outside that swallowing try/except doesn't
            # silently reopen it.
            raise ExternalAPIError("MaveDB request skipped: MaveDB was confirmed offline at startup.")

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.functional_evidence.MAX_RETRIES + 1):
            try:
                response = requests.get(url, timeout=CONFIG.functional_evidence.QUERY_TIMEOUT_SECS)
                response.raise_for_status()
                HEALTH.note_success("MaveDB")
                return response
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(f"MaveDB request attempt {attempt} failed for '{url}': {exc}")
                if not is_transient_http_error(exc):
                    break
                if attempt < CONFIG.functional_evidence.MAX_RETRIES:
                    time.sleep(CONFIG.functional_evidence.RETRY_BACKOFF_SECS * attempt)
        HEALTH.note_failure("MaveDB")
        logger.warning(
            f"MaveDB request to '{url}' failed after {CONFIG.functional_evidence.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: see the sanitization note on this method's
        # offline-skip raise above.
        raise ExternalAPIError(f"MaveDB request failed after {CONFIG.functional_evidence.MAX_RETRIES} attempts")


def _first_publication(metadata: Dict[str, Any]) -> Optional[str]:
    pubs = metadata.get("primaryPublicationIdentifiers") or []
    if not pubs:
        return None
    pub = pubs[0]
    return pub.get("referenceHtml") or pub.get("title") or pub.get("identifier")


def _best_calibration(calibrations: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Prefers the first calibration with a non-empty `functionalClassifications` list."""
    for calibration in calibrations or []:
        if calibration.get("functionalClassifications"):
            return calibration
    return None


def _bucket_score(score: float, calibration: Dict[str, Any]) -> Optional[str]:
    for bucket in calibration.get("functionalClassifications") or []:
        low, high = bucket.get("range") or [None, None]
        if _in_range(
            score, low, high, bucket.get("inclusiveLowerBound", True), bucket.get("inclusiveUpperBound", True)
        ):
            return bucket.get("functionalClassification")
    return None


def _in_range(
    score: float, low: Optional[float], high: Optional[float], inclusive_lower: bool, inclusive_upper: bool
) -> bool:
    if low is not None:
        if not (score >= low if inclusive_lower else score > low):
            return False
    if high is not None:
        if not (score <= high if inclusive_upper else score < high):
            return False
    return True


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
