"""
MMSplice service: the orchestrator-facing entry point.

Given one `Variant`, this module:
  1. Checks eligibility (requirement #4) -- variant type + distance to
     the nearest overlapping exon boundary.
  2. Fetches that exon's genomic coordinates (Ensembl REST
     `overlap/region`, `feature=exon`) and the ref/alt genomic
     sequence window around it (Ensembl REST `sequence/region`, same
     API `pipeline/sequence_context.py` already uses).
  3. Runs the loaded `MMSpliceModel` + `MMSplicePredictor`.
  4. Generates interpretation text (requirement #7) and assembles the
     exact result schema requirement #6 specifies.

Never raises: every failure path (no network, no overlapping exon,
unsupported variant type, a scoring exception) returns a well-formed
result dict with `supported`/`predicted` set appropriately and a
human-readable `reason` recorded in the log -- satisfying requirement
#13 (never crash the pipeline) without silently hiding *why* a variant
wasn't scored (requirement #4: "record the reason").
"""

import time
from typing import Dict, List, Optional, Tuple

import requests

from config import CONFIG
from pipeline.models.mmsplice.cache import MMSplicePredictionCache
from pipeline.models.mmsplice.loader import MMSpliceModel
from pipeline.models.mmsplice.models import ExonAnnotation, MMSpliceRawPrediction
from pipeline.models.mmsplice.predictor import MMSplicePredictor
from pipeline.models.mmsplice.utils import distances_to_exon_boundaries, evaluate_eligibility, generate_interpretation
from pipeline.sequence_context import SequenceContextGenerator
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


def _null_result(supported: bool, predicted: bool, reason: str, runtime_ms: float = 0.0) -> Dict:
    """The schema requirement #6 specifies, with every scored field set to None."""
    return {
        "supported": supported,
        "predicted": predicted,
        "delta_logit_psi": None,
        "donor_score": None,
        "acceptor_score": None,
        "exon_skipping": None,
        "intron_retention": None,
        "alt_donor": None,
        "alt_acceptor": None,
        "confidence": None,
        "interpretation": reason if not predicted else None,
        "runtime_ms": round(runtime_ms, 3),
        "model_version": None,
        "skip_reason": reason if not predicted else None,
    }


class MMSpliceService:
    """High-level, per-variant MMSplice API used by the pipeline orchestrator."""

    def __init__(
        self,
        model: Optional[MMSpliceModel] = None,
        sequence_context_generator: Optional[SequenceContextGenerator] = None,
        species: str = "human",
        assembly: Optional[str] = None,
    ):
        self.model = model or MMSpliceModel()
        self.predictor = MMSplicePredictor(self.model)
        self.species = species
        self.assembly = assembly
        self.base_url = CONFIG.api.ENSEMBL_REST_BASE

        # Reuse the orchestrator's own SequenceContextGenerator (same
        # species/assembly, same Ensembl session, same retry/backoff
        # logic, same per-run region cache) instead of standing up a
        # second, independent Ensembl client -- MMSplice's window just
        # isn't the flank-centered-on-variant window that class's
        # public `build_context` computes, so this deliberately calls
        # its underlying `_get_region_cached` directly (same module
        # family: `pipeline.sequence_context`).
        self._seq_ctx = sequence_context_generator or SequenceContextGenerator(
            species=species, assembly=assembly
        )
        self._session = self._seq_ctx._session  # reuse the same connection pool

        self.cache = (
            MMSplicePredictionCache(max_size=CONFIG.mmsplice.CACHE_MAX_SIZE)
            if CONFIG.mmsplice.CACHE_ENABLED
            else None
        )
        # Per-run cache of exon annotations by queried region, so a VCF
        # with many nearby variants doesn't refetch overlapping exon
        # lists from Ensembl once per variant.
        self._exon_region_cache: Dict[Tuple[str, int, int], List[ExonAnnotation]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def predict(self, variant: Variant) -> Dict:
        """Score one variant for splice effect. Never raises (requirement #13)."""
        start = time.time()
        try:
            return self._predict_impl(variant, start)
        except Exception as exc:  # noqa: BLE001 - graceful degradation is the whole point of this method
            logger.error(
                f"MMSplice prediction failed unexpectedly for "
                f"{variant.chrom}:{variant.pos}{variant.ref}>{variant.alt}: {exc}"
            )
            return _null_result(
                supported=False, predicted=False,
                reason=f"internal error during MMSplice prediction: {exc}",
                runtime_ms=(time.time() - start) * 1000.0,
            )

    def predict_batch(self, variants: List[Variant]) -> List[Dict]:
        """
        Batched variant of `predict` (requirement #12). Eligibility and
        exon/window lookup still happen per variant (they're cheap,
        cached HTTP calls); the expensive Keras forward passes for
        every *eligible* variant in the batch are grouped into two
        calls per module via `MMSplicePredictor.predict_batch`.
        """
        eligible_indices: List[int] = []
        ref_windows: List[str] = []
        alt_windows: List[str] = []
        overhangs: List[Tuple[int, int]] = []
        results: List[Optional[Dict]] = [None] * len(variants)
        cache_keys: List[Optional[tuple]] = [None] * len(variants)

        for i, variant in enumerate(variants):
            start = time.time()
            try:
                prep = self._prepare(variant, start)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"MMSplice batch preparation failed for {variant.chrom}:{variant.pos}: {exc}")
                results[i] = _null_result(False, False, f"internal error: {exc}", (time.time() - start) * 1000.0)
                continue
            if isinstance(prep, dict):
                results[i] = prep  # ineligible / not predicted / cache hit
                continue
            ref_window, alt_window, overhang, cache_key = prep
            eligible_indices.append(i)
            ref_windows.append(ref_window)
            alt_windows.append(alt_window)
            overhangs.append(overhang)
            cache_keys[i] = cache_key

        if eligible_indices:
            # All eligible variants in one call share the configured
            # overhang, so batching across differing per-exon overhangs
            # (short terminal exons padded with 'N', see SeqSplitter)
            # is still correct -- each window was already built to its
            # own (possibly padded) overhang.
            raw_predictions = self.predictor.predict_batch(ref_windows, alt_windows, overhangs[0])
            for idx, raw in zip(eligible_indices, raw_predictions):
                result = self._finalize(raw)
                results[idx] = result
                if self.cache and cache_keys[idx] is not None:
                    self.cache.put(cache_keys[idx], result)

        return [r if r is not None else _null_result(False, False, "unexpected: no result produced") for r in results]

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------
    def _predict_impl(self, variant: Variant, start: float) -> Dict:
        prep = self._prepare(variant, start)
        if isinstance(prep, dict):
            return prep
        ref_window, alt_window, overhang, cache_key = prep
        raw = self.predictor.predict(ref_window, alt_window, overhang)
        result = self._finalize(raw)
        if self.cache and cache_key is not None:
            self.cache.put(cache_key, result)
        return result

    def _prepare(self, variant: Variant, start: float):
        """
        Shared eligibility + window-construction + cache-lookup logic
        for both `predict` and `predict_batch`.

        Returns either a finished result dict (ineligible variant or a
        cache hit) or a `(ref_window, alt_window, overhang, cache_key)`
        tuple ready for the predictor.
        """
        if variant.variant_type not in CONFIG.mmsplice.SUPPORTED_VARIANT_TYPES:
            return _null_result(
                supported=False, predicted=False,
                reason=(
                    f"variant_type '{variant.variant_type}' is not supported "
                    f"(supported: {', '.join(CONFIG.mmsplice.SUPPORTED_VARIANT_TYPES)})"
                ),
                runtime_ms=(time.time() - start) * 1000.0,
            )

        exons = self._fetch_overlapping_exons(variant)
        if not exons:
            return _null_result(
                supported=False, predicted=False,
                reason="no exon annotation found near this variant (Ensembl overlap/region returned none)",
                runtime_ms=(time.time() - start) * 1000.0,
            )

        best_exon, best_eligibility, best_distance = None, None, None
        fallback_reason = None
        for exon in exons:
            dist_acceptor, dist_donor = distances_to_exon_boundaries(variant.pos, exon)
            eligibility = evaluate_eligibility(
                variant_type=variant.variant_type,
                supported_variant_types=CONFIG.mmsplice.SUPPORTED_VARIANT_TYPES,
                dist_to_acceptor=dist_acceptor,
                dist_to_donor=dist_donor,
                exon_length=exon.length,
                intron_window=CONFIG.mmsplice.INTRON_WINDOW,
                exon_near_splice_window=CONFIG.mmsplice.EXON_NEAR_SPLICE_WINDOW,
            )
            distance = min(
                d for d in (dist_acceptor, dist_donor) if d is not None
            ) if (dist_acceptor is not None or dist_donor is not None) else None
            if eligibility.eligible and (best_distance is None or (distance is not None and abs(distance) < abs(best_distance))):
                best_exon, best_eligibility, best_distance = exon, eligibility, distance
            if not eligibility.eligible:
                fallback_reason = eligibility.reason

        if best_exon is None:
            return _null_result(
                supported=False, predicted=False,
                reason=fallback_reason or "no overlapping exon fell within the configured splice window",
                runtime_ms=(time.time() - start) * 1000.0,
            )

        cache_key = None
        if self.cache:
            cache_key = MMSplicePredictionCache.make_key(
                variant.chrom, variant.pos, variant.ref, variant.alt, best_exon.transcript_id
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            ref_window, alt_window, overhang = self._build_windows(variant, best_exon)
        except ExternalAPIError as exc:
            return _null_result(
                supported=True, predicted=False,
                reason=f"reference sequence fetch failed: {exc}",
                runtime_ms=(time.time() - start) * 1000.0,
            )

        return ref_window, alt_window, overhang, cache_key

    def _finalize(self, raw: MMSpliceRawPrediction) -> Dict:
        interpretation_text, category, confidence = generate_interpretation(
            delta_logit_psi=raw.delta_logit_psi,
            donor_delta=raw.donor_delta,
            acceptor_delta=raw.acceptor_delta,
            exon_skipping_score=raw.exon_skipping_score,
            intron_retention_score=raw.intron_retention_score,
            moderate_threshold=CONFIG.mmsplice.DELTA_LOGIT_PSI_MODERATE_THRESHOLD,
            strong_threshold=CONFIG.mmsplice.DELTA_LOGIT_PSI_STRONG_THRESHOLD,
            site_loss_threshold=CONFIG.mmsplice.SITE_LOSS_THRESHOLD,
            exon_skipping_threshold=CONFIG.mmsplice.EXON_SKIPPING_THRESHOLD,
            intron_retention_threshold=CONFIG.mmsplice.INTRON_RETENTION_THRESHOLD,
        )
        return {
            "supported": True,
            "predicted": True,
            "delta_logit_psi": round(raw.delta_logit_psi, 4),
            "donor_score": round(raw.donor_delta, 4),
            "acceptor_score": round(raw.acceptor_delta, 4),
            "exon_skipping": round(raw.exon_skipping_score, 4),
            "intron_retention": round(raw.intron_retention_score, 4),
            "alt_donor": round(raw.alt_donor_score, 4),
            "alt_acceptor": round(raw.alt_acceptor_score, 4),
            "confidence": confidence,
            "interpretation": interpretation_text,
            "interpretation_category": category,
            "runtime_ms": round(raw.runtime_ms, 3),
            "model_version": raw.model_version,
            "skip_reason": None,
        }

    # ------------------------------------------------------------------
    # Exon annotation + window construction
    # ------------------------------------------------------------------
    def _fetch_overlapping_exons(self, variant: Variant) -> List[ExonAnnotation]:
        chrom = self._seq_ctx._normalize_chrom(variant.chrom)
        window = CONFIG.mmsplice.INTRON_WINDOW + 1
        region_start = max(1, variant.pos - window)
        region_end = variant.pos + window
        key = (chrom, region_start, region_end)

        cached = self._exon_region_cache.get(key)
        if cached is not None:
            return cached

        url = f"{self.base_url}/overlap/region/{self.species}/{chrom}:{region_start}-{region_end}"
        params = {"feature": "exon", "content-type": "application/json"}
        if self.assembly:
            params["coord_system_version"] = self.assembly

        last_error: Optional[Exception] = None
        for attempt in range(1, CONFIG.api.MAX_RETRIES + 1):
            try:
                response = self._session.get(url, params=params, timeout=CONFIG.api.REQUEST_TIMEOUT_SECS)
                response.raise_for_status()
                payload = response.json()
                exons = [self._parse_exon_feature(chrom, feature) for feature in payload]
                exons = [e for e in exons if e is not None]
                self._exon_region_cache[key] = exons
                return exons
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning(
                    f"Attempt {attempt}/{CONFIG.api.MAX_RETRIES} to fetch exon annotation "
                    f"for {chrom}:{region_start}-{region_end} failed: {exc}"
                )
                if attempt < CONFIG.api.MAX_RETRIES:
                    time.sleep(CONFIG.api.RETRY_BACKOFF_SECS * attempt)

        logger.error(
            f"Failed to fetch exon annotation for {chrom}:{region_start}-{region_end} "
            f"after {CONFIG.api.MAX_RETRIES} attempts: {last_error}"
        )
        self._exon_region_cache[key] = []
        return []

    @staticmethod
    def _parse_exon_feature(chrom: str, feature: Dict) -> Optional[ExonAnnotation]:
        try:
            return ExonAnnotation(
                chrom=chrom,
                start=int(feature["start"]),
                end=int(feature["end"]),
                strand=int(feature["strand"]),
                exon_id=str(feature.get("exon_id") or feature.get("id") or "unknown"),
                transcript_id=str(feature.get("Parent") or feature.get("transcript_id") or "unknown"),
                gene_id=feature.get("gene_id"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _build_windows(self, variant: Variant, exon: ExonAnnotation) -> Tuple[str, str, Tuple[int, int]]:
        """
        Fetch the genomic window [exon.start - upstream_overhang,
        exon.end + downstream_overhang] (in genomic, not transcript,
        orientation -- reverse-complemented below if the exon is on
        the minus strand) and splice in the variant to build the alt
        window, exactly like `SequenceContextGenerator.build_context`
        does for its flank-based window.
        """
        window = CONFIG.mmsplice.INTRON_WINDOW
        genomic_start = max(1, exon.start - window)
        genomic_end = exon.end + window

        genomic_ref_seq = self._seq_ctx._get_region_cached(exon.chrom, genomic_start, genomic_end)

        variant_offset = variant.pos - genomic_start
        ref_len = len(variant.ref)
        genomic_alt_seq = (
            genomic_ref_seq[:variant_offset] + variant.alt + genomic_ref_seq[variant_offset + ref_len:]
        )

        upstream_overhang = exon.start - genomic_start  # acceptor-side intron bp actually present
        downstream_overhang = genomic_end - exon.end  # donor-side intron bp actually present

        if exon.strand >= 0:
            ref_window, alt_window = genomic_ref_seq, genomic_alt_seq
            overhang = (upstream_overhang, downstream_overhang)
        else:
            ref_window = self._reverse_complement(genomic_ref_seq)
            alt_window = self._reverse_complement(genomic_alt_seq)
            overhang = (downstream_overhang, upstream_overhang)

        return ref_window, alt_window, overhang

    @staticmethod
    def _reverse_complement(seq: str) -> str:
        complement = {"A": "T", "T": "A", "C": "G", "G": "C", "N": "N"}
        return "".join(complement.get(base, "N") for base in reversed(seq.upper()))
