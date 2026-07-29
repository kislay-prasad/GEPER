"""
DNA sequence context generation.

Given a variant's chromosome/position/REF/ALT, this module fetches the
reference genomic flanking sequence (via the Ensembl REST API) and
builds both the reference-allele and alternate-allele versions of the
window, which is what every downstream DNA/RNA/protein model actually
consumes.

Ensembl REST is used (rather than requiring a local reference FASTA)
so GEPER has zero local reference-genome storage requirements and
works out of the box in Colab.
"""

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from config import CONFIG
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError, SequenceGenerationError
from utils.logger import get_logger

logger = get_logger(__name__)

# A single reference-window fetch, keyed by exactly the inputs that
# determine its result: (chrom, start, end). Species/assembly are
# fixed per SequenceContextGenerator instance, so they don't need to
# be part of the key. Deliberately module-level-shaped but stored per
# instance (see __init__) -- this only ever dedupes calls the *same*
# generator would otherwise have made, never shares state across a
# different species/assembly configuration.
_RegionKey = Tuple[str, int, int]


@dataclass
class SequenceContext:
    """Reference and alternate-allele DNA windows around a variant."""

    chrom: str
    window_start: int
    window_end: int
    flank_size: int
    ref_sequence: str
    alt_sequence: str
    variant_offset: int  # 0-based offset of the variant within the window

    @property
    def length(self) -> int:
        return len(self.ref_sequence)


class SequenceContextGenerator:
    """Fetches reference genome context and builds ref/alt sequence windows."""

    def __init__(self, species: str = "human", assembly: Optional[str] = None):
        self.species = species
        self.assembly = assembly  # e.g. "GRCh38"; None lets Ensembl use its default
        self.base_url = CONFIG.api.ENSEMBL_REST_BASE

        # A plain requests.Session (rather than the module-level
        # requests.get used before) reuses the underlying TCP/TLS
        # connection across every fetch this generator makes -- same
        # requests, same retry logic, same response handling, just
        # without repaying connection setup cost on every single
        # variant. This is the one change that touches *every* fetch;
        # it changes zero response data, only how the request is sent.
        self._session = requests.Session()

        # Memoizes _fetch_region results for this generator's lifetime
        # (== one pipeline run). A multi-allelic VCF site normalizes to
        # several Variant records that all share the same POS/REF and
        # therefore the same reference window -- each one would
        # otherwise trigger an identical, wasted network round trip.
        # Populated both lazily (on cache miss in build_context) and
        # eagerly (via prefetch_regions, see below).
        self._region_cache: Dict[_RegionKey, str] = {}

    def build_context(self, variant: Variant, flank_size: Optional[int] = None) -> SequenceContext:
        """
        Fetch a `flank_size`-base window on each side of `variant` and
        splice in the REF/ALT alleles to produce both sequence variants.
        """
        flank = flank_size or CONFIG.routing.DEFAULT_FLANK_SIZE
        chrom, start, end = self.compute_window(variant, flank)

        reference_window = self._get_region_cached(chrom, start, end)

        variant_offset = variant.pos - start
        ref_len = len(variant.ref)

        window_ref_allele = reference_window[variant_offset: variant_offset + ref_len]
        if window_ref_allele.upper() != variant.ref.upper():
            logger.warning(
                f"Reference mismatch for {variant.chrom}:{variant.pos} "
                f"(VCF REF='{variant.ref}', genome='{window_ref_allele}'). "
                f"Proceeding using the genome-derived context; verify assembly "
                f"build matches your VCF (e.g. GRCh37 vs GRCh38)."
            )

        alt_sequence = (
            reference_window[:variant_offset]
            + variant.alt
            + reference_window[variant_offset + ref_len:]
        )

        return SequenceContext(
            chrom=chrom,
            window_start=start,
            window_end=end,
            flank_size=flank,
            ref_sequence=reference_window,
            alt_sequence=alt_sequence,
            variant_offset=variant_offset,
        )

    def compute_window(self, variant: Variant, flank: int) -> Tuple[str, int, int]:
        """
        The exact (chrom, start, end) a variant's reference window
        covers, given a flank size. Pulled out of build_context so
        prefetch_regions can compute the same windows up front and
        populate the cache build_context will later read from --
        both call this one formula, so there is no way for the two to
        disagree about what region a variant needs.
        """
        chrom = self._normalize_chrom(variant.chrom)
        start = max(1, variant.pos - flank)
        end = variant.pos + max(len(variant.ref), 1) - 1 + flank
        return chrom, start, end

    def fetch_reference_sequence(self, chrom: str, start: int, end: int) -> str:
        """
        Public entry point for callers that need a raw reference-genome
        window without a `Variant`/`SequenceContext` around it -- e.g.
        `pipeline/variant_normalization.py`'s left-alignment step, which
        needs to walk single reference bases upstream of a variant.
        Shares this generator's own region cache, so a normalization
        fetch and a later `build_context` fetch over the same region
        never double the network cost.
        """
        chrom = self._normalize_chrom(chrom)
        return self._get_region_cached(chrom, start, end)

    def _get_region_cached(self, chrom: str, start: int, end: int) -> str:
        key = (chrom, start, end)
        cached = self._region_cache.get(key)
        if cached is not None:
            return cached
        sequence = self._fetch_region(chrom, start, end)
        self._region_cache[key] = sequence
        return sequence

    def prefetch_regions(self, variants: List[Variant], flank_lookup) -> None:
        """
        Best-effort batch warm-up of the region cache for an entire
        variant list, using Ensembl's POST /sequence/region/:species
        batch endpoint instead of one GET per variant.

        This is purely an optimization layer: every region it
        successfully fetches is written into the exact same
        `_region_cache` that `build_context` already checks, using the
        exact same window formula (`compute_window`), so a variant
        that was prefetched produces an identical SequenceContext to
        one that wasn't. Anything this method fails to populate (a
        batch request error, an unexpected response shape, a
        suspicious sequence length) is simply left out of the cache --
        `build_context` then falls back to its normal, already-proven
        per-variant GET path for that region exactly as it always has.
        No caller-visible behavior changes; only how many network
        round trips it took to get there.

        Args:
            variants: variants about to be processed.
            flank_lookup: callable(variant) -> int, the same flank-size
                decision the orchestrator already makes per variant
                (see SequenceRouter.recommended_flank_size) so the
                prefetched window always matches what build_context
                will actually ask for.
        """
        if not CONFIG.api.ENSEMBL_ENABLE_BATCH_PREFETCH:
            return

        # De-duplicate up front -- a multi-allelic site or a VCF with
        # repeated records would otherwise be batched (and fetched)
        # more than once for the identical window.
        needed: Dict[_RegionKey, None] = {}
        for variant in variants:
            try:
                flank = flank_lookup(variant)
                key = self.compute_window(variant, flank)
            except Exception as exc:  # noqa: BLE001 - never let prefetch block a run
                logger.warning(
                    f"Skipping prefetch window computation for "
                    f"{variant.chrom}:{variant.pos}: {exc}"
                )
                continue
            if key not in self._region_cache:
                needed[key] = None

        if not needed:
            return

        keys = list(needed.keys())
        batch_size = max(1, CONFIG.api.ENSEMBL_BATCH_SIZE)
        batches = [keys[i:i + batch_size] for i in range(0, len(keys), batch_size)]

        logger.info(
            f"Prefetching {len(keys)} distinct reference window(s) from "
            f"Ensembl in {len(batches)} batch(es) of up to {batch_size} "
            f"(instead of {len(keys)} individual requests)."
        )

        fetched = 0
        for batch in batches:
            fetched += self._prefetch_batch(batch)

        logger.info(
            f"Batch prefetch populated {fetched}/{len(keys)} reference "
            f"window(s); any remainder falls back to the normal "
            f"per-variant fetch during processing."
        )

    def _prefetch_batch(self, batch: List["_RegionKey"]) -> int:
        """
        Issue one POST /sequence/region/:species request for a batch
        of (chrom, start, end) windows and populate `_region_cache`
        for every entry that comes back looking correct. Never raises:
        any failure just means this batch contributes zero cache hits
        and every one of its variants falls back to a normal GET.
        """
        region_strings = [f"{chrom}:{start}..{end}" for chrom, start, end in batch]
        url = f"{self.base_url}/sequence/region/{self.species}"
        params = {"content-type": "application/json"}
        if self.assembly:
            params["coord_system_version"] = self.assembly

        try:
            response = self._session.post(
                url,
                params=params,
                json={"regions": region_strings},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=CONFIG.api.REQUEST_TIMEOUT_SECS,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                f"Batch prefetch of {len(batch)} region(s) failed "
                f"({exc}); these will be fetched individually instead."
            )
            return 0

        if not isinstance(payload, list) or len(payload) != len(batch):
            logger.warning(
                f"Batch prefetch response shape didn't match the "
                f"request (expected {len(batch)} entries, got "
                f"{len(payload) if isinstance(payload, list) else type(payload).__name__}); "
                f"discarding this batch and falling back to individual fetches."
            )
            return 0

        populated = 0
        for (chrom, start, end), entry in zip(batch, payload):
            expected_length = end - start + 1
            sequence = entry.get("seq") if isinstance(entry, dict) else None
            if not sequence or len(sequence) != expected_length:
                # Defensive: if a batch entry doesn't look like a clean
                # match for the region we asked for (missing, empty,
                # or wrong length -- e.g. a response-ordering surprise),
                # skip caching it rather than risk splicing a
                # mismatched sequence into a variant's context. The
                # normal per-variant GET remains the source of truth.
                logger.warning(
                    f"Skipping suspicious batch entry for {chrom}:{start}-{end} "
                    f"(expected {expected_length}bp, got "
                    f"{len(sequence) if sequence else 0}bp)."
                )
                continue
            self._region_cache[(chrom, start, end)] = sequence.upper()
            populated += 1
        return populated

    def _fetch_region(self, chrom: str, start: int, end: int) -> str:
        region = f"{chrom}:{start}-{end}"
        url = f"{self.base_url}/sequence/region/{self.species}/{region}"
        params = {"content-type": "application/json"}
        if self.assembly:
            params["coord_system_version"] = self.assembly

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.api.MAX_RETRIES + 1):
            try:
                response = self._session.get(
                    url, params=params, timeout=CONFIG.api.REQUEST_TIMEOUT_SECS
                )
                response.raise_for_status()
                payload = response.json()
                sequence = payload.get("seq")
                if not sequence:
                    raise SequenceGenerationError(
                        f"Ensembl returned no sequence for region '{region}'."
                    )
                return sequence.upper()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(
                    f"Attempt {attempt}/{CONFIG.api.MAX_RETRIES} to fetch "
                    f"region '{region}' from Ensembl failed: {exc}"
                )
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)

        raise ExternalAPIError(
            f"Failed to fetch reference sequence for '{region}' from Ensembl "
            f"after {CONFIG.api.MAX_RETRIES} attempts: {last_error}"
        )

    @staticmethod
    def _normalize_chrom(chrom: str) -> str:
        """Ensembl expects '1', 'X', 'MT' (no 'chr' prefix)."""
        normalized = chrom.replace("chr", "").replace("Chr", "").replace("CHR", "")
        if normalized in ("M", "mt", "Mt"):
            normalized = "MT"
        return normalized
