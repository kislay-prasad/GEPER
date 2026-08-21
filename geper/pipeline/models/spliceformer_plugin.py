"""
SpliceFormer plugin -- real integration (not a placeholder).

--------------------------------------------------------------------
License verification summary (repeated here so this file is
self-contained, same convention as EnformerPlugin/BorzoiPlugin):

  - Official implementation: github.com/benniatli/Spliceformer,
    the repository the peer-reviewed paper's own "Code availability"
    section links to (Jonsson et al., "Transformers significantly
    improve splice site prediction", Communications Biology 7, 1-9
    (2024), https://doi.org/10.1038/s42003-024-07298-9).
  - License: MIT, confirmed by reading the repository's own `LICENSE`
    file directly (github.com/benniatli/Spliceformer/blob/main/LICENSE,
    "MIT License / Copyright (c) 2024 Benedikt Atli Jonsson"), and by
    the "MIT license" badge on the repo's own README. Both the model
    source code and the pretrained weights ("Pre-trained Weights:
    Ready-to-use PyTorch models for splice site prediction..." per the
    repo's own README) are covered -- there is no separate,
    more-restrictive weight license anywhere in the repository.
  - Note on the Zenodo archive: the Zenodo-hosted release snapshot of
    this repo (DOI 10.5281/zenodo.14019451, cited by the paper) is
    additionally tagged CC-BY-4.0 at the *archive/deposit* level --
    this is Zenodo's own metadata license for the deposit as a
    scholarly artifact, not a separate, more restrictive license on
    the code itself. The operative license for the code GEPER
    vendors/imports is the repository's own MIT `LICENSE` file, which
    governs actual use/modification/redistribution of the source
    (mirrors the same "archive-level metadata license is not the
    code's license" distinction already documented for Enformer's
    CC-BY-4.0 note in enformer_plugin.py -- there it was a separate
    dataset; here it's the archive wrapper, but the same principle
    applies: check what the notice actually attaches to).
  - Net result: both code and weights are commercially usable and
    redistributable (MIT: attribution + license notice preserved).
    No copyleft concern.

Implementation note (why this plugin looks different from Enformer/
Borzoi's `from_pretrained(...)` one-liner): unlike Enformer
(`enformer-pytorch` on PyPI) and Borzoi (`borzoi-pytorch` on PyPI),
there is no official Spliceformer PyPI package -- the upstream
repository is ~99.6% Jupyter notebooks with the actual model
definition living in two small `.py` modules
(`Code/src/model.py`, `Code/src/weight_init.py`). GEPER vendors those
two files verbatim, unmodified, under `pipeline/models/spliceformer/
vendor/` (see that package's own docstring for the vendoring policy)
and loads one official pretrained checkpoint from the same repository
via `pipeline/models/spliceformer/loader.py`. The *model* is the real,
official, unmodified upstream implementation either way -- only the
packaging mechanism differs from Enformer/Borzoi's pip-installable
wrappers, because upstream itself does not offer one.

Ensemble/evidence-aggregation note: unlike Enformer/Borzoi, this
plugin is deliberately NOT added to
`pipeline/models/ensemble.py::_ENSEMBLE_MODEL_KEYS`, and its result is
not passed to `InterpretationEngine`/ACMG PP3-PP4 evaluation or to
`report/*` -- see this module's own callers (or rather, the deliberate
absence of any) for confirmation. It is reachable via `ModelManager`
(through `pipeline.models.pending_plugins.build_default_registry()`)
for direct/programmatic use, tests, and the benchmark script
(`benchmark_spliceformer.py`), exactly like every other plugin here,
without altering any existing evidence-aggregation or report-rendering
behavior.
--------------------------------------------------------------------
"""

from typing import Any, Dict

import numpy as np
import requests
import torch

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from pipeline.models.spliceformer import loader as spliceformer_loader
from utils.auto_install import ensure_pip_package_available

# Total input window length the official checkpoints were trained
# with: SL (5,000, the number of central positions scored per
# forward pass) + CL_max (40,000, the encoder's flanking context) --
# see pipeline/models/spliceformer/loader.py for the full sourcing
# note (from the official repo's own inference notebook). This is the
# "45k" in "Spliceformer-45k".
SPLICEFORMER_TOTAL_INPUT_LENGTH = spliceformer_loader.TOTAL_INPUT_LENGTH

# Same classification thresholds pipeline/models/enformer_plugin.py,
# borzoi_plugin.py, and pipeline/models/ensemble.py already use for
# their own single-model delta-score summaries -- reused here so a
# SpliceFormer score sits on the same documented 0..~1 scale as the
# other splicing/regulatory plugins' `score`/`classification` fields,
# rather than a third, inconsistent scale.
_NO_EFFECT_THRESHOLD = 0.1
_MODERATE_EFFECT_THRESHOLD = 0.5

# Integer base encoding used by the official repo's own delta-scoring
# notebook (Code/get_clinvar_delta_for_transformer.ipynb::seqToArray):
# 0=N/other, 1=A, 2=C, 3=G, 4=T. Reused verbatim here (not reinvented)
# so GEPER's one-hot tensors are laid out exactly the way the official
# checkpoints expect: IN_MAP[0] is the all-zero "N" row, IN_MAP[1..4]
# are the one-hot A/C/G/T rows, in that exact column order.
_IN_MAP = np.asarray(
    [
        [0, 0, 0, 0],
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ],
    dtype=np.float32,
)
_BASE_TO_CODE = {"A": 1, "C": 2, "G": 3, "T": 4}

# Network-shaped failures only -- NOT bare OSError. `spliceformer_loader.
# download_checkpoint` fetches the checkpoint via `requests.get(...)`
# (see pipeline/models/spliceformer/loader.py); a real connectivity/HTTP
# failure surfaces as `requests.exceptions.RequestException` (covers
# ConnectionError/Timeout/HTTPError from `raise_for_status()`), not a
# bare OSError. Bare OSError was removed 2026-08-21: it is the same
# class Windows raises for local resource exhaustion, unrelated to the
# network -- confirmed reproducible on this project's own dev box,
# loading ESM2: `OSError: The paging file is too small for this
# operation to complete. (os error 1455)`. Catching that here and
# relabeling it "SpliceFormer model unavailable" would report a wrong
# diagnosis. A genuine local OSError now simply isn't caught in this
# block -- it propagates through PluginModel.load()/ModelManager.get()
# (pipeline/models/base.py, manager.py), both of which already fold
# `str(exc)` into their own raised message and log it at `error`, so
# the real cause stays visible rather than being relabeled a second
# time.
_NETWORK_ERROR_TYPES = (requests.exceptions.RequestException, ConnectionError, TimeoutError)


class SpliceFormerPlugin(PluginModel):
    """Real SpliceFormer integration, gated by
    `CONFIG.splicing.ENABLE_SPLICEFORMER` (defaults to enabled); vendors
    and runs the official, unmodified model source
    (`pipeline/models/spliceformer/vendor/`) with one official
    pretrained checkpoint downloaded from the upstream MIT-licensed
    repository (`pipeline/models/spliceformer/loader.py`)."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="spliceformer",
            version=(
                f"spliceformer;ref={CONFIG.splicing.SPLICEFORMER_SOURCE_REF};"
                f"checkpoint={CONFIG.splicing.SPLICEFORMER_CHECKPOINT}"
            ),
            source="https://github.com/benniatli/Spliceformer",
            license_name="MIT",
            license_url="https://github.com/benniatli/Spliceformer/blob/main/LICENSE",
            commercial_use_allowed=True,
            license_notes=(
                "Verified against the primary source: the repository's own "
                "LICENSE file (MIT, (c) 2024 Benedikt Atli Jonsson) and "
                "README license badge. Covers both the model source code "
                "and the pretrained weights (no separate weight license is "
                "published). The Zenodo archive deposit (DOI "
                "10.5281/zenodo.14019451) additionally carries a CC-BY-4.0 "
                "archive-level metadata tag; that governs the scholarly "
                "deposit as an artifact, not the code itself, which remains "
                "governed by the repository's own MIT LICENSE file -- see "
                "this module's docstring for the full reasoning. GEPER "
                "vendors the official, unmodified model.py/weight_init.py "
                "(pipeline/models/spliceformer/vendor/) and loads one "
                "official pretrained replicate checkpoint (of the ten the "
                "paper's own headline numbers average) from the pinned "
                "v1.0.0 release tag -- see "
                "pipeline/models/spliceformer/loader.py."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        # Auto-installs `einops` on first call, same rationale as
        # EnformerPlugin.is_available()/BorzoiPlugin.is_available():
        # SpliceFormer's vendored model.py has one extra pip
        # dependency beyond what GEPER already ships (torch/numpy/
        # requests) -- einops, used by its Attention module.
        if not CONFIG.splicing.ENABLE_SPLICEFORMER:
            return False
        return ensure_pip_package_available("einops", import_name="einops")

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.splicing.ENABLE_SPLICEFORMER:
            return "disabled via CONFIG.splicing.ENABLE_SPLICEFORMER (set GEPER_ENABLE_SPLICEFORMER=true to enable)"
        return "the 'einops' package is not installed and automatic installation has not been attempted yet"

    def __init__(self):
        super().__init__()
        self._weight_cache = WeightCache()

    def _load_impl(self) -> None:
        if not ensure_pip_package_available("einops", import_name="einops"):
            raise RuntimeError(
                "Automatic installation of 'einops' did not succeed in this "
                "environment (check network access to pypi.org, or install "
                "it yourself with `pip install einops`)."
            )

        model = spliceformer_loader.build_model()

        cache_dir = self._weight_cache.ensure_dir("spliceformer")
        checkpoint_name = CONFIG.splicing.SPLICEFORMER_CHECKPOINT
        checkpoint_path = cache_dir / checkpoint_name

        if not checkpoint_path.is_file():
            try:
                spliceformer_loader.download_checkpoint(
                    checkpoint_path,
                    ref=CONFIG.splicing.SPLICEFORMER_SOURCE_REF,
                    checkpoint=checkpoint_name,
                )
            except _NETWORK_ERROR_TYPES as exc:
                # Promoted from `debug` to `warning` 2026-08-21: this line
                # is the only place the real exception class/message
                # survives -- the raised RuntimeError below deliberately
                # drops it so a raw request URL never reaches the clinical
                # report (same rationale as splicebert_plugin.py's own
                # network-error branch). At `debug` it was invisible in a
                # normal run's logs.
                self.logger.warning(
                    f"SpliceFormer checkpoint fetch for '{checkpoint_name}' failed ({exc.__class__.__name__}): {exc}",
                    exc_info=True,
                )
                raise RuntimeError("SpliceFormer model unavailable") from exc

        spliceformer_loader.load_checkpoint_into(model, checkpoint_path, self.device)

        model.to(self.device)
        model.eval()
        self.model = model

    @staticmethod
    def _prepare_sequence(sequence: str, target_length: int = SPLICEFORMER_TOTAL_INPUT_LENGTH) -> str:
        """Centers `sequence` within a window of `target_length`,
        padding with 'N' (encodes to the all-zero row of `_IN_MAP`,
        the same convention the official repo's own `seqToArray`
        helper uses for out-of-bounds/ambiguous bases) or truncating
        symmetrically if it's already longer -- identical shape to
        EnformerPlugin._prepare_sequence/BorzoiPlugin's own centering
        step, reused here for consistency across all three plugins."""
        sequence = sequence.upper()
        if len(sequence) == target_length:
            return sequence
        if len(sequence) > target_length:
            excess = len(sequence) - target_length
            start = excess // 2
            return sequence[start : start + target_length]
        pad_total = target_length - len(sequence)
        pad_left = pad_total // 2
        pad_right = pad_total - pad_left
        return ("N" * pad_left) + sequence + ("N" * pad_right)

    @staticmethod
    def _one_hot_encode(sequence: str, strand: str = "+") -> torch.Tensor:
        """
        One-hot encodes an already-length-`SPLICEFORMER_TOTAL_INPUT_LENGTH`
        sequence to the model's expected `(4, length)` channel-first
        input, following the official repo's own `seqToArray`
        integer-coding + reverse-complement convention exactly (see
        `_IN_MAP`'s docstring above) rather than a GEPER-invented
        encoding. Non-ACGT characters (including 'N') map to the
        all-zero row, matching upstream.
        """
        codes = np.fromiter((_BASE_TO_CODE.get(base, 0) for base in sequence), dtype=np.int64, count=len(sequence))
        if strand == "-":
            # Reverse-complement via upstream's own trick: reverse the
            # coded array, then map code c -> (5 - c) % 5, which sends
            # A(1)<->T(4), C(2)<->G(3), and leaves N(0) at 0.
            codes = (5 - codes[::-1]) % 5
        one_hot = _IN_MAP[codes]  # (length, 4)
        tensor = torch.from_numpy(np.ascontiguousarray(one_hot.T)).float()  # (4, length)
        return tensor

    def _infer_impl(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        """
        Runs both the reference and alternate sequence windows through
        SpliceFormer and summarizes the predicted splicing-probability
        difference between them, following the official repo's own
        delta-score definition (paper Methods, "Delta score": the
        difference between alt and ref predictions; the location/
        splice-site with the largest absolute difference is the delta
        score) and the same four-way creation/disruption breakdown its
        own ClinVar analysis notebook reports
        (`get_clinvar_delta_for_transformer.ipynb`).

        Like EnformerPlugin/BorzoiPlugin, ref_seq/alt_seq are each
        independently centered/padded to the model's fixed input
        length -- for an indel, this means the two windows are not
        perfectly nucleotide-aligned beyond the immediate vicinity of
        the variant, the same accepted simplification those two
        plugins already make (see their own `_prepare_sequence`),
        not a new limitation introduced here.

        IMPORTANT CALIBRATION CAVEAT: `score`/`classification`/
        `confidence` are a straightforward, documented summary of
        SpliceFormer's raw acceptor/donor probability delta -- NOT a
        clinically calibrated score, exactly the same caveat already
        established for Enformer/Borzoi's own `_infer_impl`.
        """
        strand = kwargs.get("strand", "+")
        ref_prepared = self._prepare_sequence(ref_seq)
        alt_prepared = self._prepare_sequence(alt_seq)

        ref_tensor = self._one_hot_encode(ref_prepared, strand)
        alt_tensor = self._one_hot_encode(alt_prepared, strand)
        batch = torch.stack([ref_tensor, alt_tensor], dim=0).to(self.device)

        self.logger.debug(
            "SpliceFormer inference device check -- "
            f"model device: {self.device}, "
            f"input tensor device: {batch.device}, "
            f"input tensor shape: {tuple(batch.shape)}"
        )

        with torch.no_grad():
            out, *_ = self.model(batch)

        # out: (2, 3, SL) -- channel 0 = no-splice, 1 = acceptor, 2 = donor.
        ref_probs, alt_probs = out[0], out[1]
        delta = alt_probs - ref_probs
        acceptor_delta = delta[1]
        donor_delta = delta[2]

        acceptor_delta_np = acceptor_delta.detach().cpu().numpy()
        donor_delta_np = donor_delta.detach().cpu().numpy()

        top_a_creation = float(np.max(acceptor_delta_np))
        top_d_creation = float(np.max(donor_delta_np))
        top_a_disruption = float(-np.min(acceptor_delta_np))
        top_d_disruption = float(-np.min(donor_delta_np))

        max_abs_delta = max(abs(top_a_creation), abs(top_d_creation), abs(top_a_disruption), abs(top_d_disruption))

        if max_abs_delta < _NO_EFFECT_THRESHOLD:
            classification = "no_significant_effect"
        elif max_abs_delta < _MODERATE_EFFECT_THRESHOLD:
            classification = "moderate_effect"
        else:
            classification = "large_effect"

        # Not a calibrated probability -- see the caveat above. Bounded
        # to [0, 1] purely so it's a well-formed number for downstream
        # consumers, nothing more (same convention as Enformer/Borzoi).
        uncalibrated_signal_strength = min(1.0, max_abs_delta / 1.0)

        return {
            "score": max_abs_delta,
            "classification": classification,
            "confidence": uncalibrated_signal_strength,
            "details": {
                "strand": strand,
                "scored_window_nt": out.shape[-1],
                "acceptor_creation_delta": top_a_creation,
                "donor_creation_delta": top_d_creation,
                "acceptor_disruption_delta": top_a_disruption,
                "donor_disruption_delta": top_d_disruption,
                "calibration_status": (
                    "uncalibrated -- raw SpliceFormer acceptor/donor "
                    "probability-delta summary, not validated against "
                    "clinical ground truth"
                ),
            },
        }
