"""
MMSplice predictor: combines the loaded model's raw modular scores for
one variant's ref/alt window pair into the full set of numeric outputs
requirement #5 asks for (delta_logit_psi, per-site scores, derived
exon-skipping / intron-retention / alternative-site scores).

Everything here operates on already-fetched ref/alt window sequences
and an already-loaded `MMSpliceModel` -- it never touches the network
or Ensembl annotation lookups (that's `service.py`'s job) and never
loads model weights itself (that's `loader.py`'s job), so it stays
simply testable with plain strings.
"""

import time
from typing import List, Tuple

from config import CONFIG
from pipeline.models.mmsplice.loader import MMSpliceModel
from pipeline.models.mmsplice.models import MMSpliceRawPrediction
from pipeline.models.mmsplice.utils import predict_delta_logit_psi
from utils.logger import get_logger

logger = get_logger(__name__)


class MMSplicePredictor:
    """Runs both alleles through the loaded MMSplice submodels and derives every scored field."""

    def __init__(self, model: MMSpliceModel):
        self.model = model

    def predict(
        self,
        ref_window: str,
        alt_window: str,
        overhang: Tuple[int, int],
    ) -> MMSpliceRawPrediction:
        start = time.time()

        ref_scores = self.model.score_modular(ref_window, overhang)
        alt_scores = self.model.score_modular(alt_window, overhang)

        delta_logit_psi = predict_delta_logit_psi(ref_scores, alt_scores)

        acceptor_intron_delta = alt_scores.acceptor_intron - ref_scores.acceptor_intron
        acceptor_delta = alt_scores.acceptor - ref_scores.acceptor
        exon_delta = alt_scores.exon - ref_scores.exon
        donor_delta = alt_scores.donor - ref_scores.donor
        donor_intron_delta = alt_scores.donor_intron - ref_scores.donor_intron

        exon_skipping_score = self._exon_skipping_score(acceptor_delta, exon_delta, donor_delta)
        intron_retention_score = self._intron_retention_score(acceptor_intron_delta, donor_intron_delta)

        alt_donor_score = self._cryptic_site_scan(
            module_name="donor", window_sequence=alt_window, overhang=overhang, is_donor=True
        )
        alt_acceptor_score = self._cryptic_site_scan(
            module_name="acceptor", window_sequence=alt_window, overhang=overhang, is_donor=False
        )

        runtime_ms = (time.time() - start) * 1000.0

        return MMSpliceRawPrediction(
            ref_scores=ref_scores,
            alt_scores=alt_scores,
            delta_logit_psi=delta_logit_psi,
            donor_delta=donor_delta,
            acceptor_delta=acceptor_delta,
            exon_delta=exon_delta,
            acceptor_intron_delta=acceptor_intron_delta,
            donor_intron_delta=donor_intron_delta,
            alt_donor_score=alt_donor_score,
            alt_acceptor_score=alt_acceptor_score,
            exon_skipping_score=exon_skipping_score,
            intron_retention_score=intron_retention_score,
            model_version=self.model.model_version,
            runtime_ms=runtime_ms,
        )

    def predict_batch(
        self,
        ref_windows: List[str],
        alt_windows: List[str],
        overhang: Tuple[int, int],
    ) -> List[MMSpliceRawPrediction]:
        """
        Batched variant of `predict` (requirement #12): scores every
        ref window and every alt window across the whole list in two
        Keras calls per module (via `MMSpliceModel.score_modular_batch`)
        instead of two calls per variant, then derives every field the
        same way `predict` does, per item.

        The nearby-cryptic-site scan is still run per-variant (it needs
        variant-specific local sequence slices), so batching mainly
        removes the dominant per-variant cost: the five-submodel
        forward pass for both alleles.
        """
        start = time.time()
        if not ref_windows:
            return []

        ref_scores_list = self.model.score_modular_batch(ref_windows, overhang)
        alt_scores_list = self.model.score_modular_batch(alt_windows, overhang)

        results: List[MMSpliceRawPrediction] = []
        for ref_scores, alt_scores, alt_window in zip(ref_scores_list, alt_scores_list, alt_windows):
            delta_logit_psi = predict_delta_logit_psi(ref_scores, alt_scores)
            acceptor_intron_delta = alt_scores.acceptor_intron - ref_scores.acceptor_intron
            acceptor_delta = alt_scores.acceptor - ref_scores.acceptor
            exon_delta = alt_scores.exon - ref_scores.exon
            donor_delta = alt_scores.donor - ref_scores.donor
            donor_intron_delta = alt_scores.donor_intron - ref_scores.donor_intron

            exon_skipping_score = self._exon_skipping_score(acceptor_delta, exon_delta, donor_delta)
            intron_retention_score = self._intron_retention_score(acceptor_intron_delta, donor_intron_delta)

            alt_donor_score = self._cryptic_site_scan(
                module_name="donor", window_sequence=alt_window, overhang=overhang, is_donor=True
            )
            alt_acceptor_score = self._cryptic_site_scan(
                module_name="acceptor", window_sequence=alt_window, overhang=overhang, is_donor=False
            )

            results.append(
                MMSpliceRawPrediction(
                    ref_scores=ref_scores,
                    alt_scores=alt_scores,
                    delta_logit_psi=delta_logit_psi,
                    donor_delta=donor_delta,
                    acceptor_delta=acceptor_delta,
                    exon_delta=exon_delta,
                    acceptor_intron_delta=acceptor_intron_delta,
                    donor_intron_delta=donor_intron_delta,
                    alt_donor_score=alt_donor_score,
                    alt_acceptor_score=alt_acceptor_score,
                    exon_skipping_score=exon_skipping_score,
                    intron_retention_score=intron_retention_score,
                    model_version=self.model.model_version,
                    runtime_ms=(time.time() - start) * 1000.0 / max(len(ref_windows), 1),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Derived scores. These are GEPER-side heuristics built *on top of*
    # MMSplice's official modular deltas -- MMSplice itself does not
    # publish a single "exon skipping probability" or "intron retention
    # score" field, so these are documented explicitly as derived
    # combinations (transparent, configurable, and reproducible from
    # the raw per-module deltas also returned alongside them) rather
    # than presented as an official MMSplice output.
    # ------------------------------------------------------------------
    @staticmethod
    def _exon_skipping_score(acceptor_delta: float, exon_delta: float, donor_delta: float) -> float:
        """
        High when acceptor and donor site strength both drop together
        (a coordinated loss of both splice sites bounding the exon is
        the classical signature of exon skipping), scaled by how much
        the exon module's own score also drops.
        """
        coordinated_loss = min(-acceptor_delta, -donor_delta)
        if coordinated_loss <= 0:
            return 0.0
        exon_factor = 1.0 + max(0.0, -exon_delta) / 10.0
        return coordinated_loss * exon_factor

    @staticmethod
    def _intron_retention_score(acceptor_intron_delta: float, donor_intron_delta: float) -> float:
        """
        High when both intronic modules' scores rise together (the
        intron increasingly "looks like" an exon to both intron-side
        models) -- the modular-score signature intron retention leaves.
        """
        coordinated_gain = min(acceptor_intron_delta, donor_intron_delta)
        return max(0.0, coordinated_gain)

    def _cryptic_site_scan(
        self, module_name: str, window_sequence: str, overhang: Tuple[int, int], is_donor: bool
    ) -> float:
        """
        Scans a small number of nearby candidate frames on either side
        of the annotated splice site within the already-fetched alt
        window, scoring each with the real donor/acceptor submodel
        (never a placeholder), and returns the strongest nearby score
        found -- evidence for "alternative donor/acceptor usage"
        (requirement #5's alt_donor / alt_acceptor). This does not
        attempt to enumerate every GT/AG motif genome-wide; it is a
        local, bounded scan (`CONFIG.mmsplice.CRYPTIC_SCAN_RANGE` bp on
        each side, documented as a heuristic, not an exhaustive cryptic-
        splice-site caller).
        """
        scan_range = CONFIG.mmsplice.CRYPTIC_SCAN_RANGE
        frame_len = 18 if is_donor else 53
        intronl_len, intronr_len = overhang

        # Canonical boundary offset within `window_sequence`, in
        # transcript (5'->3') coordinates: the exon starts right after
        # the acceptor-side overhang.
        boundary_offset = intronl_len if not is_donor else len(window_sequence) - intronr_len

        candidates: List[str] = []
        offsets: List[int] = []
        for shift in range(-scan_range, scan_range + 1):
            if is_donor:
                start = boundary_offset - 5 + shift  # donor frame = 5bp exon + 13bp intron
            else:
                start = boundary_offset - 50 + shift  # acceptor frame = 50bp intron + 3bp exon
            end = start + frame_len
            if start < 0 or end > len(window_sequence):
                continue
            candidates.append(window_sequence[start:end])
            offsets.append(shift)

        if not candidates:
            return 0.0

        try:
            scores = self.model.score_single_module_batch(module_name, candidates)
        except Exception as exc:  # noqa: BLE001 - never let the cryptic-site scan crash a prediction
            logger.warning(f"Cryptic {'donor' if is_donor else 'acceptor'} site scan failed: {exc}")
            return 0.0

        # Exclude the exact canonical offset (shift == 0) from the max,
        # since that is the reference site itself, already captured by
        # alt_scores.donor / alt_scores.acceptor -- this field is
        # specifically about *alternative* (non-canonical) sites.
        non_canonical_scores = [s for s, shift in zip(scores, offsets) if shift != 0]
        return max(non_canonical_scores) if non_canonical_scores else 0.0
