"""
Enformer plugin -- real integration (not a placeholder).

--------------------------------------------------------------------
License verification summary (see the conversation's dedicated
license audit for the full sourcing; repeated here briefly so this
file is self-contained):

  - Wrapper code (`enformer-pytorch`, github.com/lucidrains/
    enformer-pytorch): MIT license, confirmed via its own LICENSE
    file, setup.py, and PyPI page.
  - Weights: DeepMind's official Enformer weights, as hosted at the
    exact repo this plugin downloads from
    (`CONFIG.splicing.ENFORMER_HF_REPO`, default
    `EleutherAI/enformer-official-rough`), are CC-BY-4.0 -- confirmed
    directly against that repo's own HuggingFace model-card
    frontmatter (`license: cc-by-4.0`), whose body also states these
    are "the official weights released by Deepmind, ported over to
    Pytorch." (CORRECTED 2026-08-08: an earlier version of this note
    instead cited Google's own Kaggle Models license field for
    `deepmind/enformer`, Apache-2.0 -- that is a *different*
    distribution channel than the one this plugin's code actually
    points at; see LICENSE_AUDIT.md's "Full-catalogue re-verification"
    section for the full re-trace.) This is distinct from the
    enformer/README.md's "model predictions...CC-BY 4.0" line, which
    refers specifically to DeepMind's separately-published precomputed
    1000-Genomes variant-effect-score dataset -- a different artifact
    that happens to share the same license, not the same citation.
  - Net result: both code and weights are commercially usable and
    redistributable (CC-BY-4.0 requires attribution wherever the
    weights, or predictions derived from them, are redistributed --
    the `license_notes` on `metadata()` below IS that attribution;
    MIT requires only notice/attribution preservation). No GPL-style
    copyleft concern either way.

Weights are loaded via `enformer_pytorch.from_pretrained`, which is a
thin wrapper over HuggingFace's standard `PreTrainedModel.from_pretrained`
-- so passing `cache_dir=...` gets GEPER's own on-disk weight cache
(`pipeline.models.cache.WeightCache`) for free, rather than
reimplementing HuggingFace's own caching.
--------------------------------------------------------------------
"""

from typing import Any, Dict

import torch

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from utils.auto_install import ensure_pip_package_available

# Enformer's fixed input length (196,608 bp) -- from the official
# repo/README, not guessed. Positions beyond the input are cropped
# internally by the model; shorter/undersized sequences are padded
# here with 'N' (encodes to an all-zero one-hot row, matching the
# convention documented in DeepMind's own README: "N values being all
# zeros").
ENFORMER_SEQUENCE_LENGTH = 196_608

# Errors from `from_pretrained` that indicate "couldn't reach the
# model host" (as opposed to a real bug in GEPER's own code) --
# sanitized the same way models/rna_fm.py sanitizes its own network
# failures, so no raw HTTP/host detail ever reaches a clinical report.
_NETWORK_ERROR_TYPES = (OSError, ConnectionError, TimeoutError)


class EnformerPlugin(PluginModel):
    """Real Enformer integration, gated by `CONFIG.splicing.ENABLE_ENFORMER`
    (defaults to enabled); loads DeepMind's official (CC-BY-4.0) weights
    via the MIT-licensed `enformer-pytorch` wrapper."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="enformer",
            version=f"enformer-pytorch;weights={CONFIG.splicing.ENFORMER_HF_REPO}",
            source="https://github.com/lucidrains/enformer-pytorch",
            license_name="MIT (wrapper code) / CC-BY-4.0 (official DeepMind weights)",
            license_url="https://github.com/lucidrains/enformer-pytorch/blob/main/LICENSE",
            commercial_use_allowed=True,
            license_notes=(
                "Verified against primary sources: enformer-pytorch's own "
                "LICENSE (MIT); the exact weight repo this plugin downloads "
                "(CONFIG.splicing.ENFORMER_HF_REPO, default "
                "'EleutherAI/enformer-official-rough') states 'license: "
                "cc-by-4.0' in its own HuggingFace model-card frontmatter, "
                "and its own model card confirms these are 'the official "
                "weights released by Deepmind, ported over to Pytorch'. "
                "(An earlier version of this note cited Google's Kaggle "
                "Models license field for deepmind/enformer, Apache-2.0 -- "
                "that is a different distribution channel than the one "
                "this plugin actually downloads from; corrected 2026-08-08, "
                "see LICENSE_AUDIT.md's 'Full-catalogue re-verification' "
                "section.) CC-BY-4.0 requires attribution wherever these "
                "weights or predictions derived from them are "
                "redistributed -- this note IS that attribution. The "
                "separate CC-BY-4.0 notice in DeepMind's own "
                "enformer/README.md refers to a precomputed variant-score "
                "dataset, not these weights -- a different thing that "
                "happens to share the same license, not the same citation."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        # Auto-installs on first call, same rationale as
        # RNAFMModel.is_available() / HyenaDNAModel.is_available():
        # the orchestrator's up-front availability probe should
        # reflect the environment after setup, not before it -- a
        # passive is_pip_package_installed() check here would mean
        # the enabled-by-default flag never actually gets a chance to
        # self-heal into "available" within a running process.
        if not CONFIG.splicing.ENABLE_ENFORMER:
            return False
        return ensure_pip_package_available("enformer-pytorch", import_name="enformer_pytorch")

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.splicing.ENABLE_ENFORMER:
            return "disabled via CONFIG.splicing.ENABLE_ENFORMER (set GEPER_ENABLE_ENFORMER=true to enable)"
        return "the 'enformer-pytorch' package is not installed and automatic installation has not been attempted yet"

    def __init__(self):
        super().__init__()
        self._weight_cache = WeightCache()

    def _load_impl(self) -> None:
        if not ensure_pip_package_available("enformer-pytorch", import_name="enformer_pytorch"):
            raise RuntimeError(
                "Automatic installation of 'enformer-pytorch' did not succeed "
                "in this environment (check network access to pypi.org, or "
                "install it yourself with `pip install enformer-pytorch`)."
            )

        import enformer_pytorch

        # Compatibility shim for a confirmed upstream enformer-pytorch bug,
        # not a GEPER issue: enformer_pytorch.modeling_enformer.Enformer
        # subclasses transformers.PreTrainedModel but its __init__ never
        # calls self.post_init(). transformers>=5.0 computes the
        # `all_tied_weights_keys` bookkeeping dict inside post_init() and
        # PreTrainedModel.from_pretrained() now reads it unconditionally
        # during weight loading, so on any transformers>=5 install this
        # raises `AttributeError: 'Enformer' object has no attribute
        # 'all_tied_weights_keys'` before a single real weight is loaded
        # (see e.g. huggingface/transformers#42270, #43883, and the
        # equivalent fix other third-party PreTrainedModel subclasses
        # have shipped: adding this exact attribute). Enformer ties no
        # embedding/head weights, so the correct value is the same empty
        # dict post_init() would have produced -- this changes no
        # architecture, weights, or computed output, only unblocks the
        # real `from_pretrained` weight-loading path on newer transformers.
        Enformer = enformer_pytorch.modeling_enformer.Enformer
        if not hasattr(Enformer, "all_tied_weights_keys"):
            Enformer.all_tied_weights_keys = getattr(Enformer, "_tied_weights_keys", None) or {}

        repo_id = CONFIG.splicing.ENFORMER_HF_REPO
        cache_dir = self._weight_cache.ensure_dir("enformer")

        try:
            model = enformer_pytorch.from_pretrained(repo_id, cache_dir=str(cache_dir))
        except _NETWORK_ERROR_TYPES as exc:
            self.logger.debug(
                f"Enformer weight fetch for '{repo_id}' failed ({exc.__class__.__name__}): {exc}",
                exc_info=True,
            )
            raise RuntimeError("Enformer model unavailable") from exc

        model.to(self.device)
        model.eval()
        self.model = model

    @staticmethod
    def _prepare_sequence(sequence: str, target_length: int = ENFORMER_SEQUENCE_LENGTH) -> str:
        """Centers `sequence` within a window of `target_length`,
        padding with 'N' (encodes to all-zero, per DeepMind's own
        documented convention) or truncating symmetrically if it's
        already longer than the model's fixed input length."""
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

    def _infer_impl(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        """
        Runs both the reference and alternate sequence through
        Enformer and summarizes the predicted regulatory/expression
        difference between them.

        IMPORTANT CALIBRATION CAVEAT: `score`/`classification`/
        `confidence` here are a straightforward, documented summary
        of Enformer's raw track-level output delta -- NOT a
        clinically calibrated splicing/regulatory-impact score. This
        mirrors the caveat already established for GEPER's broader
        Interpretation Engine (no clinical validation yet); treating
        this as calibrated PP3/BP4 evidence without a dedicated
        validation pass would misrepresent what's actually been
        checked.
        """
        head = kwargs.get("head", "human")
        ref_prepared = self._prepare_sequence(ref_seq)
        alt_prepared = self._prepare_sequence(alt_seq)

        # ROOT CAUSE OF THE DEVICE-MISMATCH BUG:
        # enformer_pytorch.modeling_enformer.Enformer.forward() builds
        # the one-hot input tensor on CPU (via its module-level
        # `str_to_one_hot`, which uses a CPU-resident embedding table)
        # and then calls `x.to(self.device)` -- but `.to()` returns a
        # new tensor rather than moving in place, and that line never
        # reassigns `x`. So when the plugin passed raw sequence
        # strings straight into `self.model([...], head=head)`, the
        # tensor Enformer actually ran its GPU-resident weights against
        # stayed on CPU, producing exactly the reported
        # "Input type (torch.FloatTensor) and weight type
        # (torch.cuda.FloatTensor) should be the same" error. This is
        # an upstream enformer-pytorch bug, not something GEPER can
        # patch inside that library -- so the fix here is to do the
        # one-hot encoding ourselves (using the same helper the library
        # uses internally) and move the resulting tensor to
        # `self.device` *before* it ever reaches `self.model(...)`,
        # matching the pattern BorzoiPlugin._infer_impl already uses.
        # Once `x` is already on the right device, that no-op
        # `x.to(self.device)` inside Enformer's forward() is harmless.
        import enformer_pytorch

        input_tensor = enformer_pytorch.str_to_one_hot([ref_prepared, alt_prepared]).to(self.device)

        self.logger.debug(
            "Enformer inference device check -- "
            f"model device: {self.device}, "
            f"input tensor device: {input_tensor.device}, "
            f"input tensor shape: {tuple(input_tensor.shape)}"
        )

        with torch.no_grad():
            outputs = self.model(input_tensor, head=head)

        ref_tracks, alt_tracks = outputs[0], outputs[1]
        delta = alt_tracks - ref_tracks
        mean_abs_delta = delta.abs().mean().item()
        max_abs_delta = delta.abs().max().item()

        if mean_abs_delta < 0.1:
            classification = "no_significant_effect"
        elif mean_abs_delta < 0.5:
            classification = "moderate_effect"
        else:
            classification = "large_effect"

        # Not a calibrated probability -- see the caveat above.
        # Bounded to [0, 1] purely so it's a well-formed number for
        # downstream consumers, nothing more.
        uncalibrated_signal_strength = min(1.0, mean_abs_delta / 1.0)

        return {
            "score": mean_abs_delta,
            "classification": classification,
            "confidence": uncalibrated_signal_strength,
            "details": {
                "head": head,
                "max_abs_delta": max_abs_delta,
                "num_tracks": ref_tracks.shape[-1],
                "calibration_status": (
                    "uncalibrated -- raw Enformer track-delta summary, not validated against clinical ground truth"
                ),
            },
        }
