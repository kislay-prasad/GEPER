"""
Conservation-score query providers.

Two concrete sources per score type (PhyloP, PhastCons -- GERP++ to
follow the same pattern), matching `pipeline/gnomad/provider.py`'s own
"local indexed file, API fallback" shape:

  - `LocalBigWigProvider`: queries a local per-base bigWig track file
    (the exact format UCSC itself publishes, e.g. `hg38.phyloP100way.bw`)
    via the `bigWigSummary` CLI (part of UCSC's "kent" command-line
    tools, https://hgdownload.soe.ucsc.edu/admin/exe/) -- the same
    "subprocess over an indexed file, no native-extension Python
    binding" approach `pipeline/gnomad/provider.py::LocalIndexedGnomadProvider`
    uses `tabix` for. Never downloads/builds the bigWig itself (these
    are multi-GB genome-wide tracks) -- strictly a query layer over a
    path the deployer has already provisioned. One instance per score
    type (`score_type="phylop"` / `"phastcons"`), since each is a
    separate track/file.
  - `UCSCApiProvider`: falls back to UCSC's public REST API
    (https://api.genome.ucsc.edu/getData/track) when no local track is
    configured/available. Verified directly against real requests
    before this module was written (see pipeline/conservation/
    __init__.py's own docstring for the exact confirmed values,
    including the fact that PhyloP's track name differs by build
    ("phyloP100way" on hg38, "phyloP100wayAll" on hg19) while
    PhastCons's does not (both "phastCons100way").

`CompositeConservationProvider` runs each active score type's own
local-first/API-fallback chain independently and merges the results
into ONE `ConservationAnnotation` per variant (so a caller gets both
`phylop_score` and `phastcons_score` from a single `query()` call) --
never raises, matching gnomAD/ClinGen's graceful-degradation policy.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.conservation.models import ConservationAnnotation
from pipeline.conservation.utils import normalize_chrom, ucsc_genome_id
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)

# Which `ConservationAnnotation` field each score type populates.
SCORE_FIELD = {"phylop": "phylop_score", "phastcons": "phastcons_score", "gerp": "gerp_score"}

# UCSC track name per (score_type, build). PhyloP's hg19 track is
# named differently from its hg38 one ("phyloP100wayAll" vs
# "phyloP100way" -- the same 100-way alignment, republished under hg19
# coordinates under upstream's own differing naming); PhastCons uses
# the identical name on both builds. Both facts verified against real
# UCSC API responses / its own `/list/tracks` endpoint before being
# hardcoded here -- never guessed.
TRACK_BY_BUILD = {
    "phylop": {"GRCh38": "phyloP100way", "GRCh37": "phyloP100wayAll"},
    "phastcons": {"GRCh38": "phastCons100way", "GRCh37": "phastCons100way"},
}

# Which `ConservationConfig` field holds the local bigWig path for
# each (score_type, build) pair -- passed explicitly to each provider
# instance (see `CompositeConservationProvider._build_default_sub_providers`)
# rather than string-templated/dynamically-guessed, so the mapping
# stays grep-able and typo-proof.
_LOCAL_PATH_CONFIG_FIELD = {
    "phylop": {"GRCh38": "PHYLOP_GRCH38_LOCAL_BIGWIG", "GRCh37": "PHYLOP_GRCH37_LOCAL_BIGWIG"},
    "phastcons": {"GRCh38": "PHASTCONS_GRCH38_LOCAL_BIGWIG", "GRCh37": "PHASTCONS_GRCH37_LOCAL_BIGWIG"},
    "gerp": {"GRCh38": "GERP_GRCH38_LOCAL_BIGWIG", "GRCh37": "GERP_GRCH37_LOCAL_BIGWIG"},
}


class ConservationProviderBase:
    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[ConservationAnnotation]:
        """Return a ConservationAnnotation, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local indexed bigWig
# ---------------------------------------------------------------------------


class LocalBigWigProvider(ConservationProviderBase):
    """Queries one local conservation-track bigWig file (`score_type`:
    "phylop" or "phastcons") via
    `bigWigSummary <file> <chrom> <start> <end> 1` (one data point over
    the 1-base region), the minimal invocation UCSC's own kent-tools
    documentation describes for a single-position lookup."""

    name = "local_bigwig"

    def __init__(self, score_type: str, bigwigsummary_binary: str = "bigWigSummary"):
        self.score_type = score_type
        self.score_field = SCORE_FIELD[score_type]
        self.bigwigsummary_binary = bigwigsummary_binary
        self._checked_binary: Optional[bool] = None

    def is_available(self) -> bool:
        # Windows caveat (verified, not just suspected): UCSC's kent-tools
        # ship no official Windows build of bigWigSummary at all (Linux/
        # macOS only), and even a deployer-supplied extension-less POSIX-
        # style binary placed on PATH is invisible to `shutil.which` on
        # Windows -- it only probes `<name>.<ext>` for each PATHEXT
        # extension (.EXE/.COM/.BAT/...), never the bare name, so a
        # bigWigSummary without an extension is never found there
        # (confirmed locally: a chmod'd, PATH-visible extension-less file
        # returns None from `shutil.which` on Windows). A real
        # `bigWigSummary.exe` (Windows-native or a suitable port) would
        # still be found normally since `.EXE` is in the default
        # PATHEXT. Net effect: on a bare Windows deployment this local
        # track is realistically always unavailable, which is exactly
        # the safe behavior here -- it degrades to the GERP API fallback
        # (`MyVariantGerpProvider`) rather than failing, so this is
        # documented as a known limitation, not treated as a bug to work
        # around.
        if self._checked_binary is None:
            import shutil

            self._checked_binary = shutil.which(self.bigwigsummary_binary) is not None
        return self._checked_binary

    def _local_path_for_build(self, build: str) -> Optional[str]:
        field_name = _LOCAL_PATH_CONFIG_FIELD[self.score_type].get(build)
        if not field_name:
            return None
        return getattr(CONFIG.conservation, field_name, "") or None

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[ConservationAnnotation]:
        local_path = self._local_path_for_build(build)
        if not local_path:
            return None  # not configured for this build/score type -- let the composite provider try the API
        if not self.is_available():
            logger.warning(
                f"A local {self.score_type} bigWig is configured for {build} ('{local_path}') but "
                f"'{self.bigwigsummary_binary}' is not on PATH; skipping the local track for this lookup."
            )
            return None

        query_chrom = normalize_chrom(chrom)
        # bigWigSummary's coordinates are 0-based half-open, like BED --
        # a 1-based VCF `pos` covers 0-based [pos-1, pos).
        start, end = pos - 1, pos

        try:
            proc = subprocess.run(
                [self.bigwigsummary_binary, local_path, query_chrom, str(start), str(end), "1"],
                capture_output=True,
                text=True,
                timeout=CONFIG.conservation.QUERY_TIMEOUT_SECS,
            )
        except FileNotFoundError:
            logger.warning(
                f"'{self.bigwigsummary_binary}' is not available on PATH after all; skipping local {self.score_type} track."
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                f"Local {self.score_type} bigWigSummary query timed out for {query_chrom}:{pos} against '{local_path}'."
            )
            return ConservationAnnotation.from_error(chrom, pos, ref, alt, build, "local bigwig query timed out")

        stdout = (proc.stdout or "").strip()
        if proc.returncode != 0 or not stdout:
            # bigWigSummary exits non-zero (and prints "no data") for a
            # position with no coverage in the track -- a legitimate
            # "not found", not an error, the same way an empty tabix
            # result means "no gnomAD record" rather than a failure.
            if "no data" in (proc.stderr or "").lower():
                return ConservationAnnotation.not_found(chrom, pos, ref, alt, build, self.name)
            logger.warning(
                f"Local {self.score_type} bigWigSummary query failed (exit {proc.returncode}) for {query_chrom}:{pos}: "
                f"{(proc.stderr or '').strip()[:300]}"
            )
            return ConservationAnnotation.from_error(
                chrom, pos, ref, alt, build, f"bigWigSummary exit {proc.returncode}"
            )

        try:
            score = float(stdout)
        except ValueError:
            return ConservationAnnotation.from_error(
                chrom, pos, ref, alt, build, f"unparseable bigWigSummary output: {stdout!r}"
            )

        return ConservationAnnotation(
            chrom=chrom,
            pos=pos,
            ref=ref,
            alt=alt,
            build=build,
            source=self.name,
            found=True,
            **{self.score_field: score},
        )


# ---------------------------------------------------------------------------
# UCSC REST API fallback
# ---------------------------------------------------------------------------


class UCSCApiProvider(ConservationProviderBase):
    """Queries UCSC's public REST API directly for one score type
    (`score_type`: "phylop" or "phastcons"), for deployments without a
    locally-provisioned bigWig file. Retry/backoff/timeout mirrors
    `pipeline/gnomad/provider.py::GraphQLGnomadProvider`."""

    name = "ucsc_api"

    def __init__(self, score_type: str, endpoint: Optional[str] = None):
        self.score_type = score_type
        self.score_field = SCORE_FIELD[score_type]
        self.endpoint = endpoint or CONFIG.conservation.UCSC_API_ENDPOINT

    def is_available(self) -> bool:
        return bool(CONFIG.conservation.ENABLE_UCSC_API_FALLBACK) and not CONFIG.conservation.OFFLINE_MODE

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[ConservationAnnotation]:
        if not self.is_available():
            return None
        track = TRACK_BY_BUILD[self.score_type].get(build)
        if track is None:
            return ConservationAnnotation.from_error(
                chrom, pos, ref, alt, build, f"no {self.score_type} track known for build '{build}'"
            )

        genome = ucsc_genome_id(build)
        query_chrom = normalize_chrom(chrom)
        start, end = pos - 1, pos  # UCSC API uses 0-based half-open, same as bigWig itself

        try:
            payload = self._get(genome, track, query_chrom, start, end)
        except ExternalAPIError as exc:
            logger.warning(f"UCSC conservation API query failed for {query_chrom}:{pos} ({track}): {exc}")
            return ConservationAnnotation.from_error(chrom, pos, ref, alt, build, str(exc))

        if payload.get("error") is not None:
            # UCSC's API returns HTTP 200 with an {"error": ...} body
            # for some bad-request cases in addition to a 4xx status --
            # treated the same way either way.
            return ConservationAnnotation.from_error(chrom, pos, ref, alt, build, str(payload["error"]))

        values = payload.get(track) or []
        if not values:
            return ConservationAnnotation.not_found(chrom, pos, ref, alt, build, self.name)

        score = values[0].get("value")
        if score is None:
            return ConservationAnnotation.not_found(chrom, pos, ref, alt, build, self.name)

        return ConservationAnnotation(
            chrom=chrom,
            pos=pos,
            ref=ref,
            alt=alt,
            build=build,
            source=self.name,
            found=True,
            **{self.score_field: float(score)},
        )

    def _get(self, genome: str, track: str, chrom: str, start: int, end: int) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        params = {"genome": genome, "track": track, "chrom": chrom, "start": start, "end": end}
        for attempt in range(1, CONFIG.conservation.MAX_RETRIES + 1):
            try:
                response = requests.get(self.endpoint, params=params, timeout=CONFIG.conservation.QUERY_TIMEOUT_SECS)
                if response.status_code >= 500:
                    response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"UCSC conservation API request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.conservation.MAX_RETRIES:
                    time.sleep(CONFIG.conservation.RETRY_BACKOFF_SECS * attempt)
        logger.warning(
            f"UCSC conservation API request to '{self.endpoint}' failed after "
            f"{CONFIG.conservation.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: the message actually raised reaches
        # `ConservationAnnotation.from_error(...)` -> `conservation_result`'s
        # `error` field, embedded verbatim in geper_results.json AND
        # rendered unconditionally into every Markdown report's "Stage
        # Warnings / Errors" section via `pipeline/orchestrator.py::
        # _run_conservation_stage`'s `errors.append(...)`. Must not embed
        # `self.endpoint`/`last_error`; full detail goes to the log line
        # above only.
        raise ExternalAPIError(f"UCSC conservation API request failed after {CONFIG.conservation.MAX_RETRIES} attempts")


# ---------------------------------------------------------------------------
# GERP++ API fallback -- MyVariant.info, NOT UCSC
# ---------------------------------------------------------------------------
#
# GERP++ is NOT hosted as a UCSC track under any name for either build
# (confirmed by walking the full `/list/tracks` response tree for both
# hg19 and hg38 -- zero matches for "gerp"), and Ensembl's own REST API
# does not expose conservation scores either (confirmed against
# Ensembl's own FAQ / mailing list: GERP is only distributed as bigWig
# files on their FTP site or via the Compara Perl API, no REST
# endpoint). MyVariant.info (https://myvariant.info), a public
# variant-annotation aggregator that re-publishes dbNSFP's precomputed
# GERP++ RS score, is the one live, queryable source verified to
# actually work: `GET /v1/variant/chr17:g.7674858C>T?assembly=hg38`
# returned a real score (5.01) for a known TP53 coding position before
# this class was written. Two real, load-bearing quirks confirmed
# against live requests, not guessed from documentation alone:
#   - hg19 is MyVariant.info's *default* (no `assembly` param needed);
#     hg38 requires `assembly=hg38` explicitly.
#   - Coverage is dbNSFP's own (effectively every possible coding
#     SNV, not full genome-wide base-pair coverage the way UCSC's
#     phyloP/phastCons bigWigs are) -- a position genuinely absent
#     from dbNSFP correctly 404s, treated as "not found", not an error.


class MyVariantGerpProvider(ConservationProviderBase):
    """Queries MyVariant.info's dbNSFP-derived GERP++ RS score. SNVs
    only -- MyVariant.info's `chrN:g.POSREF>ALT` id format needs full
    HGVS delins/del/dup construction for indels, which this provider
    deliberately does not attempt (returns None for a non-SNV,
    exactly like `LocalBigWigProvider`/`UCSCApiProvider` return None
    for "not configured for this build" -- "try the next provider",
    not an error)."""

    name = "myvariant_gerp"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = endpoint or CONFIG.conservation.MYVARIANT_API_ENDPOINT

    def is_available(self) -> bool:
        return bool(CONFIG.conservation.ENABLE_MYVARIANT_API_FALLBACK) and not CONFIG.conservation.OFFLINE_MODE

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[ConservationAnnotation]:
        if not self.is_available():
            return None
        if len(ref) != 1 or len(alt) != 1 or ref.upper() not in "ACGT" or alt.upper() not in "ACGT":
            return None  # not a plain SNV -- see class docstring

        query_chrom = normalize_chrom(chrom)
        variant_id = f"{query_chrom}:g.{pos}{ref.upper()}>{alt.upper()}"
        params = {"fields": "dbnsfp.gerp"}
        if build == "GRCh38":
            params["assembly"] = "hg38"
        # GRCh37 needs no param -- MyVariant.info's own default.

        try:
            status_code, payload = self._get(variant_id, params)
        except ExternalAPIError as exc:
            logger.warning(f"MyVariant.info GERP query failed for {variant_id}: {exc}")
            return ConservationAnnotation.from_error(chrom, pos, ref, alt, build, str(exc))

        if status_code == 404:
            return ConservationAnnotation.not_found(chrom, pos, ref, alt, build, self.name)

        gerp = (payload.get("dbnsfp") or {}).get("gerp")
        score = self._extract_score(gerp)
        if score is None:
            return ConservationAnnotation.not_found(chrom, pos, ref, alt, build, self.name)

        return ConservationAnnotation(
            chrom=chrom,
            pos=pos,
            ref=ref,
            alt=alt,
            build=build,
            source=self.name,
            found=True,
            gerp_score=float(score),
        )

    @staticmethod
    def _extract_score(gerp: Any) -> Optional[float]:
        """
        Parses MyVariant.info's actual `dbnsfp.gerp` response shape --
        verified against real live responses before this was written,
        NOT assumed: `{"91_mammals": {"rankscore": ..., "score": 5.01}}`,
        i.e. the RS score is nested one level deeper than the field
        name alone would suggest, under a `"91_mammals"` sub-key (the
        number of mammalian species dbNSFP's own GERP computation
        used) -- confirmed identical across two independent test
        variants (chr17:g.7674858C>T and the API's own documented
        chr7:g.55241707G>T example) before hardcoding this key.
        `gerp` can also come back as a list (MyVariant.info's
        documented behavior for other dbNSFP-sourced fields when more
        than one row maps to the same position/allele) -- the first
        entry is used, matching how this provider already takes "the"
        score for a position rather than disambiguating transcripts.
        """
        if isinstance(gerp, list):
            gerp = gerp[0] if gerp else None
        if not isinstance(gerp, dict):
            return None
        mammals = gerp.get("91_mammals")
        if isinstance(mammals, list):
            mammals = mammals[0] if mammals else None
        if isinstance(mammals, dict) and mammals.get("score") is not None:
            return mammals["score"]
        # Defensive fallback only -- not verified against a real
        # response, kept in case a future dbNSFP/MyVariant.info schema
        # revision flattens this field; never silently preferred over
        # the verified "91_mammals" shape above.
        return gerp.get("score")

    def _get(self, variant_id: str, params: Dict[str, Any]):
        import urllib.parse

        last_error: Optional[Exception] = None
        url = f"{self.endpoint}/{urllib.parse.quote(variant_id, safe=':')}"
        for attempt in range(1, CONFIG.conservation.MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=CONFIG.conservation.QUERY_TIMEOUT_SECS)
                if response.status_code == 404:
                    return 404, {}
                if response.status_code >= 500:
                    response.raise_for_status()
                return response.status_code, response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"MyVariant.info GERP request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.conservation.MAX_RETRIES:
                    time.sleep(CONFIG.conservation.RETRY_BACKOFF_SECS * attempt)
        logger.warning(
            f"MyVariant.info GERP request to '{url}' failed after "
            f"{CONFIG.conservation.MAX_RETRIES} attempts: {last_error}",
            exc_info=last_error,
        )
        # Round 28: see the sanitization note on `UCSCApiProvider._get`
        # above -- same reach, same fix.
        raise ExternalAPIError(f"MyVariant.info GERP request failed after {CONFIG.conservation.MAX_RETRIES} attempts")


# ---------------------------------------------------------------------------
# Per-score-type local-first/API-fallback chain
# ---------------------------------------------------------------------------


class _SingleScoreProvider:
    """Local-first, API-fallback chain for exactly one score type --
    the same "try local, then try API" logic
    `pipeline.gnomad.provider.CompositeGnomadProvider` applies across
    its whole result, applied here per score type so
    `CompositeConservationProvider` can run several independently and
    merge them (see its own docstring)."""

    def __init__(self, score_type: str, local_provider: LocalBigWigProvider, api_provider: ConservationProviderBase):
        self.score_type = score_type
        self.local_provider = local_provider
        self.api_provider = api_provider

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[ConservationAnnotation]:
        if CONFIG.conservation.OFFLINE_MODE:
            return self._try(self.local_provider, chrom, pos, ref, alt, build)

        result = self._try(self.local_provider, chrom, pos, ref, alt, build)
        if result is not None:
            return result
        return self._try(self.api_provider, chrom, pos, ref, alt, build)

    def _try(
        self, provider: ConservationProviderBase, chrom: str, pos: int, ref: str, alt: str, build: str
    ) -> Optional[ConservationAnnotation]:
        try:
            return provider.query(chrom, pos, ref, alt, build)
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            logger.error(f"Conservation provider '{provider.name}' ({self.score_type}) raised unexpectedly: {exc}")
            return ConservationAnnotation.from_error(chrom, pos, ref, alt, build, f"{provider.name} raised: {exc}")


# ---------------------------------------------------------------------------
# Composite: every active score type, merged into one annotation
# ---------------------------------------------------------------------------


class CompositeConservationProvider:
    """Runs each configured score type's own local-first/API-fallback
    chain (`_SingleScoreProvider`) and merges the results into ONE
    `ConservationAnnotation` -- `found=True` if ANY score type
    resolved, `source` lists which source answered each one, `error`
    is only set if EVERY score type failed outright (a genuine partial
    result -- e.g. PhyloP found, PhastCons errored -- is still
    `found=True`, matching this framework's "report what you have,
    never let one failure hide a partial success" policy elsewhere,
    e.g. `pipeline.models.ensemble.EnsembleManager`'s own 0/1/2-model
    routing)."""

    def __init__(self, sub_providers: Optional[Dict[str, _SingleScoreProvider]] = None):
        self.sub_providers = sub_providers or self._build_default_sub_providers()

    @staticmethod
    def _build_default_sub_providers() -> Dict[str, _SingleScoreProvider]:
        return {
            score_type: _SingleScoreProvider(
                score_type,
                local_provider=LocalBigWigProvider(
                    score_type, bigwigsummary_binary=CONFIG.conservation.BIGWIGSUMMARY_BINARY
                ),
                # GERP++ has no UCSC track (verified -- see
                # MyVariantGerpProvider's own docstring); every other
                # integrated score type does.
                api_provider=MyVariantGerpProvider() if score_type == "gerp" else UCSCApiProvider(score_type),
            )
            for score_type in SCORE_FIELD
        }

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> ConservationAnnotation:
        merged = ConservationAnnotation(chrom=chrom, pos=pos, ref=ref, alt=alt, build=build, source="none", found=False)
        answered_by: List[str] = []
        error_messages: List[str] = []

        for score_type, sub_provider in self.sub_providers.items():
            annotation = sub_provider.query(chrom, pos, ref, alt, build)
            if annotation is None:
                continue
            if annotation.error is not None:
                error_messages.append(f"{score_type}: {annotation.error}")
                continue
            if annotation.found:
                score_field = SCORE_FIELD[score_type]
                setattr(merged, score_field, getattr(annotation, score_field))
                merged.found = True
                answered_by.append(f"{score_type}:{annotation.source}")

        merged.source = "+".join(answered_by) if answered_by else "none"
        if error_messages and not merged.found:
            merged.error = "; ".join(error_messages)
        return merged

    def batch_query(self, variants: List[tuple]) -> List[ConservationAnnotation]:
        """`variants`: list of (chrom, pos, ref, alt, build) tuples. Thread-pooled, matching CompositeGnomadProvider.batch_query."""
        if not variants:
            return []
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=CONFIG.conservation.MAX_CONCURRENT_ASYNC) as executor:
            return list(executor.map(lambda v: self.query(*v), variants))
