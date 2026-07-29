"""
gnomAD query providers.

Two concrete sources, matching requirement #4 ("Support Local indexed
database. GraphQL fallback."):

  - `LocalIndexedGnomadProvider`: queries a local, bgzip'd + tabix-
    indexed gnomAD "sites" VCF (exactly the file format Broad
    publishes -- e.g. `gnomad.genomes.v4.1.sites.chr1.vcf.bgz` plus its
    `.tbi`), using the `tabix` CLI. Same integration shape as
    `models/alphamissense.py`'s tabix-indexed catalogue lookup, and
    faster + more robust than any live network call once provisioned.
  - `GraphQLGnomadProvider`: falls back to gnomAD's public GraphQL API
    (https://gnomad.broadinstitute.org/api) when no local index is
    configured/available for the variant's build, with retry/backoff/
    timeout matching `database/clinvar_client.py::_request_json`.

`CompositeGnomadProvider` tries local first, then GraphQL, honoring
offline mode, and never raises out of `query()`/`batch_query()` for an
ordinary "not found" or network failure -- callers always get a
`GnomadAnnotation` back (possibly with `.error` set), matching the
graceful-degradation policy every other external stage
(ClinVar/dbSNP/BLAST) already follows.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests

from config import CONFIG
from pipeline.gnomad.models import GnomadAnnotation, POPULATIONS, PopulationFrequency
from pipeline.gnomad.utils import (
    extract_global_counts_from_info,
    extract_population_counts_from_info,
    gnomad_variant_id,
    normalize_chrom,
    parse_vcf_info_field,
)
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


class GnomadProviderBase:
    """Common interface every gnomAD data source implements."""

    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - trivial override points
        raise NotImplementedError

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[GnomadAnnotation]:
        """Return a GnomadAnnotation, or None if this provider cannot answer at all (try the next one)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local indexed database
# ---------------------------------------------------------------------------


class LocalIndexedGnomadProvider(GnomadProviderBase):
    """
    Queries a local, tabix-indexed gnomAD sites VCF via the `tabix`
    CLI -- the same "download once, index once, query via subprocess"
    approach `models/alphamissense.py` uses for its catalogue, chosen
    for the same reason: no pysam/cyvcf2 native-extension dependency
    (see `pipeline/vcf_parser.py`'s module docstring).

    Unlike AlphaMissense's catalogue, gnomAD's own published site VCFs
    already ship bgzip'd + tabix-indexed (a `.tbi` sidecar is part of
    Broad's standard release), so this provider never downloads or
    builds an index itself -- it is strictly a query layer over a path
    the deployer has already provisioned (`GnomadConfig.GRCH38_LOCAL_VCF`
    / `GRCH37_LOCAL_VCF`), exactly matching requirement #4's "Local
    indexed database" as one of two configurable, independent sources.
    """

    name = "local_index"

    def __init__(self, tabix_binary: str = "tabix"):
        self.tabix_binary = tabix_binary
        self._checked_binary: Optional[bool] = None

    def is_available(self) -> bool:
        if self._checked_binary is None:
            import shutil

            self._checked_binary = shutil.which(self.tabix_binary) is not None
        return self._checked_binary

    def _local_path_for_build(self, build: str) -> Optional[str]:
        if build == "GRCh37":
            path = CONFIG.gnomad.GRCH37_LOCAL_VCF
        else:
            path = CONFIG.gnomad.GRCH38_LOCAL_VCF
        return path or None

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[GnomadAnnotation]:
        local_path = self._local_path_for_build(build)
        if not local_path:
            return None  # not configured for this build -- let the composite provider try GraphQL
        if not self.is_available():
            logger.warning(
                f"A local gnomAD VCF is configured for {build} ('{local_path}') but the "
                f"'{self.tabix_binary}' binary is not on PATH; skipping the local index for this lookup."
            )
            return None

        # gnomAD's GRCh38 site VCFs use 'chr'-prefixed contigs; its
        # legacy GRCh37 liftover VCFs do not. Never guessed silently --
        # `normalize_chrom` makes the convention explicit per build.
        with_chr = build == "GRCh38"
        query_chrom = normalize_chrom(chrom, with_chr_prefix=with_chr)
        region = f"{query_chrom}:{pos}-{pos}"

        try:
            proc = subprocess.run(
                [self.tabix_binary, local_path, region],
                capture_output=True,
                text=True,
                timeout=CONFIG.gnomad.QUERY_TIMEOUT_SECS,
            )
        except FileNotFoundError:
            logger.warning(f"'{self.tabix_binary}' is not available on PATH after all; skipping local gnomAD index.")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"Local gnomAD tabix query timed out for {region} against '{local_path}'.")
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, "local index query timed out")

        if proc.returncode != 0:
            logger.warning(
                f"Local gnomAD tabix query failed (exit {proc.returncode}) for {region}: "
                f"{(proc.stderr or '').strip()[:300]}"
            )
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, f"tabix exit {proc.returncode}")

        for line in proc.stdout.splitlines():
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 8:
                continue
            record_ref, record_alt, info_str = fields[3], fields[4], fields[7]
            # A site VCF can list multiple ALTs comma-separated at the
            # same position; only the caller's exact ref/alt is a match.
            if record_ref.upper() != ref.upper() or alt.upper() not in {a.upper() for a in record_alt.split(",")}:
                continue
            return _annotation_from_info_line(chrom, pos, ref, alt, build, parse_vcf_info_field(info_str), self.name)

        return GnomadAnnotation.not_found(chrom, pos, ref, alt, build, self.name)


def _annotation_from_info_line(
    chrom: str, pos: int, ref: str, alt: str, build: str, info: Dict[str, str], source: str
) -> GnomadAnnotation:
    globals_ = extract_global_counts_from_info(info)
    populations = extract_population_counts_from_info(info)

    breakdown = {}
    for pop, (ac, an, hom, hemi) in populations.items():
        if ac is None and an is None and hom is None and hemi is None:
            continue
        breakdown[pop] = PopulationFrequency(population=pop, ac=ac, an=an, hom=hom, hemi=hemi)

    annotation = GnomadAnnotation(
        chrom=chrom,
        pos=pos,
        ref=ref,
        alt=alt,
        build=build,
        source=source,
        found=True,
        genome_af=globals_.get("af"),
        ac=globals_.get("ac"),
        an=globals_.get("an"),
        hom=globals_.get("hom"),
        hemi=globals_.get("hemi"),
        population_breakdown=breakdown,
    )
    annotation.annotate_highest_population()
    return annotation


# ---------------------------------------------------------------------------
# GraphQL fallback
# ---------------------------------------------------------------------------

_GRAPHQL_QUERY = """
query GeperVariant($variantId: String!, $datasetId: DatasetId!) {
  variant(variantId: $variantId, dataset: $datasetId) {
    genome { ac an af homozygote_count hemizygote_count populations { id ac an homozygote_count hemizygote_count } }
    exome { ac an af homozygote_count hemizygote_count populations { id ac an homozygote_count hemizygote_count } }
  }
}
"""

_DATASET_BY_BUILD = {
    "GRCh38": "gnomad_r4",
    "GRCh37": "gnomad_r2_1",
}


class GraphQLGnomadProvider(GnomadProviderBase):
    """
    Queries gnomAD's public GraphQL API directly, for deployments
    without a locally-provisioned indexed VCF. Retry/backoff/timeout
    mirrors `database/clinvar_client.py::_request_json` exactly, so
    every external-API stage in GEPER behaves identically under
    transient failure.
    """

    name = "graphql"

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = endpoint or CONFIG.gnomad.GRAPHQL_ENDPOINT

    def is_available(self) -> bool:
        return bool(CONFIG.gnomad.ENABLE_GRAPHQL_FALLBACK) and not CONFIG.gnomad.OFFLINE_MODE

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[GnomadAnnotation]:
        if not self.is_available():
            return None
        dataset_id = _DATASET_BY_BUILD.get(build)
        if dataset_id is None:
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, f"no gnomAD dataset known for build '{build}'")

        variant_id = gnomad_variant_id(chrom, pos, ref, alt)
        try:
            payload = self._post(variant_id, dataset_id)
        except ExternalAPIError as exc:
            logger.warning(f"gnomAD GraphQL query failed for {variant_id}: {exc}")
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, str(exc))

        variant = (payload.get("data") or {}).get("variant")
        if not variant:
            return GnomadAnnotation.not_found(chrom, pos, ref, alt, build, self.name)

        return _annotation_from_graphql(chrom, pos, ref, alt, build, variant, self.name)

    def _post(self, variant_id: str, dataset_id: str) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        body = {"query": _GRAPHQL_QUERY, "variables": {"variantId": variant_id, "datasetId": dataset_id}}
        for attempt in range(1, CONFIG.gnomad.MAX_RETRIES + 1):
            try:
                response = requests.post(self.endpoint, json=body, timeout=CONFIG.gnomad.QUERY_TIMEOUT_SECS)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(f"gnomAD GraphQL request attempt {attempt} failed: {exc}")
                if attempt < CONFIG.gnomad.MAX_RETRIES:
                    time.sleep(CONFIG.gnomad.RETRY_BACKOFF_SECS * attempt)
        raise ExternalAPIError(f"gnomAD GraphQL request to '{self.endpoint}' failed after {CONFIG.gnomad.MAX_RETRIES} attempts: {last_error}")


def _annotation_from_graphql(
    chrom: str, pos: int, ref: str, alt: str, build: str, variant: Dict[str, Any], source: str
) -> GnomadAnnotation:
    genome = variant.get("genome") or {}
    exome = variant.get("exome") or {}

    def _pop_breakdown(block: Dict[str, Any]) -> Dict[str, PopulationFrequency]:
        breakdown = {}
        for entry in block.get("populations") or []:
            pop_id = (entry.get("id") or "").lower()
            if pop_id not in POPULATIONS:
                # gnomAD's API also returns sex-stratified rows (e.g.
                # "afr_XX") interleaved in the same list; this
                # integration reports population-level (not
                # population+sex) breakdowns per requirement #3, so
                # those are skipped here rather than mis-merged into
                # the wrong bucket.
                continue
            ac, an = entry.get("ac"), entry.get("an")
            breakdown[pop_id] = PopulationFrequency(
                population=pop_id,
                ac=ac,
                an=an,
                hom=entry.get("homozygote_count"),
                hemi=entry.get("hemizygote_count"),
            )
        return breakdown

    # Genome and exome each contribute their own population breakdown;
    # merge additively (never overwrite a population genome already
    # supplied) so the combined breakdown reflects whichever call set
    # actually has data for that population -- some populations are
    # only well-represented in one call set.
    breakdown = _pop_breakdown(genome)
    for pop, freq in _pop_breakdown(exome).items():
        breakdown.setdefault(pop, freq)

    annotation = GnomadAnnotation(
        chrom=chrom,
        pos=pos,
        ref=ref,
        alt=alt,
        build=build,
        source=source,
        found=True,
        genome_af=genome.get("af"),
        exome_af=exome.get("af"),
        ac=genome.get("ac") if genome.get("ac") is not None else exome.get("ac"),
        an=genome.get("an") if genome.get("an") is not None else exome.get("an"),
        hom=genome.get("homozygote_count") if genome.get("homozygote_count") is not None else exome.get("homozygote_count"),
        hemi=genome.get("hemizygote_count") if genome.get("hemizygote_count") is not None else exome.get("hemizygote_count"),
        population_breakdown=breakdown,
    )
    annotation.annotate_highest_population()
    return annotation


# ---------------------------------------------------------------------------
# Composite: local-first, GraphQL-fallback, sync + async batch
# ---------------------------------------------------------------------------


class CompositeGnomadProvider:
    """
    Tries the local indexed provider first (when configured for the
    variant's build), then the GraphQL fallback (unless offline mode
    is on) -- requirement #4's "Query Sources: Local indexed database.
    GraphQL fallback." Never raises: any provider failure is logged
    and reported inside the returned `GnomadAnnotation.error`, matching
    ClinVar/dbSNP/BLAST's graceful-degradation policy.
    """

    def __init__(
        self,
        local_provider: Optional[LocalIndexedGnomadProvider] = None,
        graphql_provider: Optional[GraphQLGnomadProvider] = None,
        max_concurrent_async: Optional[int] = None,
    ):
        self.local_provider = local_provider or LocalIndexedGnomadProvider(tabix_binary=CONFIG.gnomad.TABIX_BINARY)
        self.graphql_provider = graphql_provider or GraphQLGnomadProvider()
        self.max_concurrent_async = max_concurrent_async or CONFIG.gnomad.MAX_CONCURRENT_ASYNC

    def query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> GnomadAnnotation:
        if CONFIG.gnomad.OFFLINE_MODE:
            result = self._try(self.local_provider, chrom, pos, ref, alt, build)
            if result is not None:
                return result
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, "offline mode: no local gnomAD index configured for this build")

        result = self._try(self.local_provider, chrom, pos, ref, alt, build)
        if result is not None:
            return result

        result = self._try(self.graphql_provider, chrom, pos, ref, alt, build)
        if result is not None:
            return result

        return GnomadAnnotation.not_found(chrom, pos, ref, alt, build, "none")

    @staticmethod
    def _try(provider: GnomadProviderBase, chrom: str, pos: int, ref: str, alt: str, build: str) -> Optional[GnomadAnnotation]:
        try:
            return provider.query(chrom, pos, ref, alt, build)
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            logger.error(f"gnomAD provider '{provider.name}' raised unexpectedly: {exc}")
            return GnomadAnnotation.from_error(chrom, pos, ref, alt, build, f"{provider.name} raised: {exc}")

    # -- batch: sync, thread-pooled ------------------------------------

    def batch_query(self, variants: List[tuple]) -> List[GnomadAnnotation]:
        """
        `variants`: list of (chrom, pos, ref, alt, build) tuples.
        Concurrent (thread-pooled, since each query is I/O-bound: a
        tabix subprocess or an HTTP round trip) -- requirement #4's
        "Batch querying". Bounded by `max_concurrent_async` so a large
        VCF cannot open unbounded simultaneous subprocesses/sockets.
        """
        if not variants:
            return []
        with ThreadPoolExecutor(max_workers=self.max_concurrent_async) as executor:
            return list(executor.map(lambda v: self.query(*v), variants))

    # -- batch: async ----------------------------------------------------

    async def async_query(self, chrom: str, pos: int, ref: str, alt: str, build: str) -> GnomadAnnotation:
        """
        Async single-variant query (requirement #4's "Async
        querying"). The underlying provider calls are themselves
        synchronous (a `tabix` subprocess, or `requests` for GraphQL --
        matching every other external client in this codebase, which
        is deliberately `requests`-based rather than `aiohttp`/`httpx`
        for consistency), so this offloads the blocking call to a
        worker thread via `asyncio.to_thread` rather than duplicating
        the provider logic in a second, async-native HTTP client.
        """
        return await asyncio.to_thread(self.query, chrom, pos, ref, alt, build)

    async def async_batch_query(self, variants: List[tuple]) -> List[GnomadAnnotation]:
        """Async batch/many-at-once querying, bounded by an asyncio.Semaphore matching `max_concurrent_async`."""
        if not variants:
            return []
        semaphore = asyncio.Semaphore(self.max_concurrent_async)

        async def _bounded(v: tuple) -> GnomadAnnotation:
            async with semaphore:
                return await self.async_query(*v)

        return await asyncio.gather(*[_bounded(v) for v in variants])
