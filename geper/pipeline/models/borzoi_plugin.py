"""
Borzoi plugin -- real integration (not a placeholder).

--------------------------------------------------------------------
License verification summary (full sourcing in the conversation's
dedicated license audit; repeated here so this file is self-contained
and so the constraint below is enforced in code, not just documented):

  - Calico's own repository code (github.com/calico/borzoi): Apache-2.0
    (confirmed via the repo's own license badge/LICENSE file).
  - Calico's own original TensorFlow weights (the `.h5` files hosted
    at storage.googleapis.com/seqnn-share/borzoi/...): NO explicit,
    separate weight license was found anywhere in Calico's own
    materials. The Apache-2.0 badge is a repository-level designation
    and was not found to explicitly extend to these externally-hosted
    binary files the way DeepMind's Kaggle model card explicitly does
    for Enformer. GEPER MUST NOT use these files -- see the guard in
    `_load_impl` below, which enforces this rather than relying on
    convention alone.
  - The PyTorch port (github.com/johahi/borzoi-pytorch): confirmed
    directly against the repo's own license badge and raw `LICENSE`
    file -- **Apache-2.0**, not MIT. (CORRECTED 2026-08-08: an earlier
    version of this note claimed MIT here, cited to wording in the
    peer-reviewed Flashzoi paper -- that wording does not match either
    primary source directly; see LICENSE_AUDIT.md's "Full-catalogue
    re-verification" section for the full re-trace.)
  - The PyTorch port's weights (mirrored to HuggingFace under the
    `johahi` org, e.g. `johahi/borzoi-replicate-0`): confirmed
    directly against that repo's own HuggingFace model-card
    frontmatter -- **CC-BY-4.0**, not MIT either. The peer-reviewed
    Flashzoi paper (Bioinformatics, Oxford Academic) additionally
    confirms these weights were "ported with permission" from Calico,
    which is still accurate and unaffected by the license-label
    correction above.
  - Net result: GEPER integrates Borzoi exclusively through this
    verified path (`borzoi-pytorch`, Apache-2.0, + a `johahi/...`
    HuggingFace repo id, CC-BY-4.0). Both are commercially usable and
    redistributable (Apache-2.0: notice/attribution preservation;
    CC-BY-4.0: attribution wherever the weights, or predictions
    derived from them, are redistributed -- the `license_notes` on
    `metadata()` below IS that attribution). `CONFIG.splicing.BORZOI_HF_REPO`
    defaults to a `johahi/` repo, and `_load_impl` refuses to load
    anything outside that namespace, so a misconfiguration can't
    silently point this at an unverified weight source.
--------------------------------------------------------------------
"""

from typing import Any, Dict

import torch

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from utils.auto_install import ensure_pip_package_available

# Borzoi's official fixed input length: 524,288 bp ("524kb input
# sequences" -- github.com/calico/borzoi's own README). Not guessed.
BORZOI_SEQUENCE_LENGTH = 524_288

# Only weights mirrored under this HuggingFace namespace have an
# explicit, verified commercial-use license (see module docstring).
# Calico's original GCS `.h5` checkpoints are never referenced here.
_ALLOWED_BORZOI_HF_NAMESPACE = "johahi/"

_BASE_TO_INDEX = {"A": 0, "C": 1, "G": 2, "T": 3}

_NETWORK_ERROR_TYPES = (OSError, ConnectionError, TimeoutError)


class BorzoiLicenseGuardError(RuntimeError):
    """Raised if `CONFIG.splicing.BORZOI_HF_REPO` is ever pointed
    somewhere other than the verified `johahi/` weight mirror (CC-BY-4.0)
    -- a configuration mistake, not a network failure, so it is
    intentionally not treated as "gracefully skip and retry later"."""


class BorzoiPlugin(PluginModel):
    """Real Borzoi integration. Disabled by default
    (`CONFIG.splicing.ENABLE_BORZOI`); once enabled, this loads ONLY
    the CC-BY-4.0-licensed `johahi` HuggingFace weight mirror via the
    Apache-2.0-licensed `borzoi-pytorch` package -- never Calico's
    original `.h5` files."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="borzoi",
            version=f"borzoi-pytorch;weights={CONFIG.splicing.BORZOI_HF_REPO}",
            source="https://github.com/johahi/borzoi-pytorch",
            license_name="Apache-2.0 (wrapper code) / CC-BY-4.0 (johahi-mirrored weights)",
            license_url="https://github.com/johahi/borzoi-pytorch/blob/main/LICENSE",
            commercial_use_allowed=True,
            license_notes=(
                "Verified directly against primary sources: "
                "johahi/borzoi-pytorch's own GitHub license badge and raw "
                "LICENSE file (Apache-2.0); the johahi-mirrored weight "
                "repo's own HuggingFace model-card frontmatter (e.g. "
                "johahi/borzoi-replicate-0, 'license: cc-by-4.0'). "
                "CORRECTED 2026-08-08: an earlier version of this note "
                "claimed MIT for both, cited to wording in the "
                "peer-reviewed Flashzoi paper (Bioinformatics, Oxford "
                "Academic) -- that wording does not match either primary "
                "source directly; see LICENSE_AUDIT.md's 'Full-catalogue "
                "re-verification' section for the full re-trace. The "
                "paper's separate claim that these weights were 'ported "
                "with Calico's permission' is still accurate and "
                "unaffected by this correction. CC-BY-4.0 requires "
                "attribution wherever the weights, or predictions derived "
                "from them, are redistributed -- this note IS that "
                "attribution. Calico's own original .h5 checkpoints "
                "(GCS-hosted) have no equivalent explicit weight license "
                "and are never used by this plugin -- enforced in "
                "_load_impl, not just documented."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        # Auto-installs on first call -- see EnformerPlugin.is_available()
        # for the full rationale (mirrors RNAFMModel/HyenaDNAModel).
        if not CONFIG.splicing.ENABLE_BORZOI:
            return False
        return ensure_pip_package_available("borzoi-pytorch", import_name="borzoi_pytorch")

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.splicing.ENABLE_BORZOI:
            return "disabled via CONFIG.splicing.ENABLE_BORZOI (set GEPER_ENABLE_BORZOI=true to enable)"
        return "the 'borzoi-pytorch' package is not installed and automatic installation has not been attempted yet"

    def __init__(self):
        super().__init__()
        self._weight_cache = WeightCache()

    def _load_impl(self) -> None:
        repo_id = CONFIG.splicing.BORZOI_HF_REPO
        if not repo_id.startswith(_ALLOWED_BORZOI_HF_NAMESPACE):
            raise BorzoiLicenseGuardError(
                f"CONFIG.splicing.BORZOI_HF_REPO='{repo_id}' is outside the "
                f"verified CC-BY-4.0-licensed '{_ALLOWED_BORZOI_HF_NAMESPACE}' "
                "namespace. Calico's original weights have no equivalent "
                "explicit commercial license, so this plugin refuses to "
                "load anything else. See this module's docstring."
            )

        if not ensure_pip_package_available("borzoi-pytorch", import_name="borzoi_pytorch"):
            raise RuntimeError(
                "Automatic installation of 'borzoi-pytorch' did not succeed "
                "in this environment (check network access to pypi.org, or "
                "install it yourself with `pip install borzoi-pytorch`)."
            )

        import borzoi_pytorch

        # Compatibility shim for the same upstream-subclass issue
        # `pipeline/models/enformer_plugin.py::_load_impl` already shims
        # for Enformer (see that method's own comment for the full
        # explanation and the transformers issue references):
        # borzoi_pytorch.Borzoi also subclasses transformers.PreTrainedModel
        # without calling self.post_init(), so on transformers>=5 --
        # confirmed live, 2026-08-17 -- PreTrainedModel.from_pretrained's
        # _finalize_model_loading -> _move_missing_keys_from_meta_to_device
        # unconditionally reads `self.all_tied_weights_keys.keys()`, which
        # Borzoi never has set, raising `AttributeError: 'Borzoi' object
        # has no attribute 'all_tied_weights_keys'` before any real weight
        # loads. Borzoi ties no embedding/head weights either, so the
        # correct value is the same empty dict post_init() would have
        # produced -- this changes no architecture, weights, or computed
        # output, only unblocks the real from_pretrained weight-loading
        # path on newer transformers, identically to the Enformer shim.
        Borzoi = borzoi_pytorch.Borzoi
        if not hasattr(Borzoi, "all_tied_weights_keys"):
            Borzoi.all_tied_weights_keys = getattr(Borzoi, "_tied_weights_keys", None) or {}

        cache_dir = self._weight_cache.ensure_dir("borzoi")

        try:
            model = borzoi_pytorch.Borzoi.from_pretrained(repo_id, cache_dir=str(cache_dir))
        except _NETWORK_ERROR_TYPES as exc:
            self.logger.debug(
                f"Borzoi weight fetch for '{repo_id}' failed ({exc.__class__.__name__}): {exc}",
                exc_info=True,
            )
            raise RuntimeError("Borzoi model unavailable") from exc

        model.to(self.device)
        model.eval()
        self.model = model

    @staticmethod
    def _one_hot_encode(sequence: str, target_length: int = BORZOI_SEQUENCE_LENGTH) -> torch.Tensor:
        """
        Centers/pads/truncates `sequence` to `target_length`, then
        one-hot encodes to Borzoi's documented input shape (4, L)
        (channels-first -- see `Borzoi.forward`'s own docstring:
        "Input DNA sequence tensor of shape (N, 4, L)"). Unlike
        Enformer's `enformer-pytorch`, `borzoi-pytorch` has no
        built-in string-to-one-hot helper, so this is GEPER's own
        encoder, not a copy of any upstream code.
        """
        sequence = sequence.upper()
        if len(sequence) > target_length:
            excess = len(sequence) - target_length
            start = excess // 2
            sequence = sequence[start : start + target_length]
        elif len(sequence) < target_length:
            pad_total = target_length - len(sequence)
            pad_left = pad_total // 2
            pad_right = pad_total - pad_left
            sequence = ("N" * pad_left) + sequence + ("N" * pad_right)

        one_hot = torch.zeros(4, target_length, dtype=torch.float32)
        for position, base in enumerate(sequence):
            index = _BASE_TO_INDEX.get(base)
            if index is not None:
                one_hot[index, position] = 1.0
            # 'N' (or any other symbol) is left as all-zero, matching
            # the same convention Enformer's own documentation uses.
        return one_hot

    def _infer_impl(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        """
        Same calibration caveat as EnformerPlugin._infer_impl: this
        is a raw track-delta summary, not a clinically calibrated
        score. See that method's docstring for the full rationale.
        """
        is_human = kwargs.get("is_human", True)
        ref_tensor = self._one_hot_encode(ref_seq).unsqueeze(0).to(self.device)
        alt_tensor = self._one_hot_encode(alt_seq).unsqueeze(0).to(self.device)

        with torch.no_grad():
            ref_out = self.model(ref_tensor, is_human=is_human)
            alt_out = self.model(alt_tensor, is_human=is_human)

        delta = alt_out - ref_out
        mean_abs_delta = delta.abs().mean().item()
        max_abs_delta = delta.abs().max().item()

        if mean_abs_delta < 0.1:
            classification = "no_significant_effect"
        elif mean_abs_delta < 0.5:
            classification = "moderate_effect"
        else:
            classification = "large_effect"

        uncalibrated_signal_strength = min(1.0, mean_abs_delta / 1.0)

        return {
            "score": mean_abs_delta,
            "classification": classification,
            "confidence": uncalibrated_signal_strength,
            "details": {
                "is_human": is_human,
                "max_abs_delta": max_abs_delta,
                "num_tracks": ref_out.shape[1],
                "calibration_status": (
                    "uncalibrated -- raw Borzoi track-delta summary, not validated against clinical ground truth"
                ),
            },
        }
