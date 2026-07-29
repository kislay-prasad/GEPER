"""
Evo 2 wrapper.

Arc Institute's "Evo 2" (a StripedHyena-2 "Vortex"-architecture DNA
foundation model pretrained across all domains of life) replaces
Nucleotide Transformer as the model GEPER routes to for variants that
need cross-species evolutionary context or fall in structurally
complex regions (see config.RoutingConfig.COMPLEX_CONTEXT_VARIANT_TYPES),
since general short-context calls are cheaper to serve via HyenaDNA.

IMPORTANT -- how this is actually implemented, and why: Evo 2 is NOT a
transformers.AutoModel-compatible checkpoint, even though its weights
are hosted on Hugging Face. It is loaded exclusively through Arc
Institute's own `evo2` pip package (verified against that package's
own README/PyPI page, not assumed from the model card):

    from evo2 import Evo2
    model = Evo2('evo2_7b_base')
    input_ids = torch.tensor(model.tokenizer.tokenize(seq), dtype=torch.int)
    outputs, embeddings = model(input_ids, return_embeddings=True, layer_names=[...])

Hardware reality (also verified against Arc Institute's docs and issue
tracker, not assumed): the 40B variant, and the long-context 1M-token
'evo2_7b' checkpoint specifically, ship with
`use_fp8_input_projections=True` and require Transformer Engine + FP8
+ a Hopper-class GPU -- confirmed via
https://github.com/ArcInstitute/evo2/issues/208, where a user hit
exactly that requirement while loading plain 'evo2_7b' despite the
"light install" docs describing 7B models as TE-free. The checkpoint
that is genuinely TE-free is 'evo2_7b_base' (8K training context) --
GEPER's default (config.CONFIG.models.EVO2_VARIANT) -- but even it
still depends on `flash-attn`, which has no practical CPU build.
There is therefore no honest CPU fallback for this model, unlike
every other model in GEPER's stack (which all have a real, if slower,
CPU path). `is_available()` reflects that directly: it requires a
CUDA GPU *and* the `evo2` package (auto-installed once per process via
the same mechanism as RNA-FM's `rna-fm` dependency -- see
utils/auto_install.py) rather than pretending a CPU path exists.
Checking for a GPU first, before attempting the package install, also
avoids a slow, doomed `pip install` attempt on every CPU-only
environment.

HARDWARE FLOOR -- Turing (e.g. Tesla T4) is not supported, and this is
not fixable by pinning different package versions: `evo2`'s attention
path is FlashAttention-2 (via `vtx`), and FlashAttention-2's own
current install docs state plainly that its CUDA backend supports
"Ampere, Ada, or Hopper GPUs" (compute capability >= 8.0) and that
Turing GPUs (T4, RTX 2080) are covered only by a separate, third-party,
partial-feature project (github.com/ssiu/flash-attention-turing) that
Arc Institute does not integrate with or support for `evo2` --
confirmed directly against Dao-AILab/flash-attention's README and
PyPI page. A T4 is Turing, compute capability 7.5. On that hardware,
`pip install flash-attn==2.8.0.post2` either fails to build the CUDA
extension at all, or -- if a stale/mismatched wheel or cached build is
present -- produces a package whose Python shim imports fine but whose
compiled `flash_attn_2_cuda` extension is missing or non-functional
for this architecture, which is exactly the
`No module named 'flash_attn_2_cuda'` failure this class now detects
and reports proactively via `is_available()` / `unavailability_reason()`
below, instead of surfacing three layers deep inside `evo2`'s own
`Evo2(...)` constructor.
"""

from typing import Any, Dict

import torch

from config import CONFIG
from models.base_model import BaseGenomicModel
from utils.auto_install import ensure_pip_package_available
from utils.device_utils import get_cuda_compute_capability
from utils.exceptions import ModelLoadError

# FlashAttention-2's CUDA backend requires Ampere or newer (compute
# capability >= 8.0) -- verified against Dao-AILab/flash-attention's
# current README/PyPI page, which explicitly excludes Turing (7.x)
# GPUs including the Tesla T4 from the official CUDA build and points
# Turing users to a separate, unofficial, partial-feature project
# instead. `evo2` depends on flash-attn directly for its
# StripedHyena-2 attention kernels, so this floor is evo2's floor too.
_MIN_FLASH_ATTN_COMPUTE_CAPABILITY = (8, 0)


class Evo2Model(BaseGenomicModel):
    """Embeds DNA sequences using Evo 2 for cross-species / complex genomic context."""

    def cache_key(self) -> str:
        return "evo2"

    @classmethod
    def _unsupported_gpu_reason(cls):
        """
        Returns a human-readable reason string if the present GPU's
        compute capability is below what FlashAttention-2 (and
        therefore evo2) requires, or None if the GPU is adequate (or
        absent, which is handled separately by the CUDA-availability
        check in `is_available()`/`_load_impl()`).
        """
        capability = get_cuda_compute_capability()
        if capability is None:
            return None
        if capability < _MIN_FLASH_ATTN_COMPUTE_CAPABILITY:
            name = torch.cuda.get_device_name(0)
            return (
                f"GPU present ({name}, compute capability "
                f"{capability[0]}.{capability[1]}) but below what "
                f"FlashAttention-2 requires for its CUDA backend "
                f"(compute capability >= "
                f"{_MIN_FLASH_ATTN_COMPUTE_CAPABILITY[0]}."
                f"{_MIN_FLASH_ATTN_COMPUTE_CAPABILITY[1]}, i.e. Ampere/"
                f"Ada/Hopper). Evo 2 depends on flash-attn directly and "
                f"has no officially supported path on Turing GPUs (T4, "
                f"RTX 2080) -- see models/evo2.py module docstring for "
                f"the full citation trail. Skipping Evo 2; other routed "
                f"models (HyenaDNA) remain available."
            )
        return None

    @classmethod
    def is_available(cls) -> bool:
        if not torch.cuda.is_available():
            return False
        if cls._unsupported_gpu_reason() is not None:
            return False
        return ensure_pip_package_available("evo2")

    @classmethod
    def unavailability_reason(cls) -> str:
        if not torch.cuda.is_available():
            return (
                "no CUDA GPU detected (Evo 2 has no practical CPU path)"
            )
        gpu_reason = cls._unsupported_gpu_reason()
        if gpu_reason is not None:
            return gpu_reason
        return "not installed"

    def _load_impl(self):
        if not torch.cuda.is_available():
            raise ModelLoadError(
                "Evo 2 has no practical CPU inference path (it depends on "
                "flash-attn, which has no real CPU build) -- a CUDA GPU is "
                "required. This model is skipped automatically when no GPU "
                "is present; this error should only be reachable if loading "
                "was forced explicitly."
            )
        gpu_reason = self._unsupported_gpu_reason()
        if gpu_reason is not None:
            raise ModelLoadError(
                "Evo 2 cannot run on this GPU: " + gpu_reason + " This is "
                "a hardware/kernel-support limit, not a GEPER configuration "
                "issue -- it is not fixable by changing requirements.txt "
                "pins. This model is skipped automatically once detected "
                "via is_available(); this error should only be reachable "
                "if loading was forced explicitly."
            )
        if not ensure_pip_package_available("evo2"):
            raise ModelLoadError(
                "Evo 2 requires the official 'evo2' pip package, and "
                "automatic installation did not succeed in this "
                "environment. See https://github.com/ArcInstitute/evo2 for "
                "manual install instructions (it additionally requires "
                "flash-attn, and a matching CUDA-enabled torch build)."
            )

        from evo2 import Evo2

        variant = CONFIG.models.EVO2_VARIANT
        # The Evo2 wrapper object itself is directly callable
        # (`evo2_model(input_ids, ...)`) and is not itself a plain
        # nn.Module (no named_parameters()/named_buffers()), so it's
        # kept as a dedicated attribute rather than assumed to be
        # something BaseGenomicModel's generic hooks can introspect.
        # self.model/self.tokenizer are still set (to the wrapper and
        # its tokenizer) so any generic logging elsewhere in the base
        # class has something real to point at.
        self._evo2 = Evo2(variant)
        self.model = self._evo2
        self.tokenizer = self._evo2.tokenizer
        self._dtype = torch.bfloat16

    def _verify_materialized(self) -> None:
        # See _load_impl: the Evo2 wrapper is not an nn.Module, so the
        # base class's generic meta-device parameter/buffer scan
        # doesn't apply to it. Evo2(...) either fully constructs (the
        # package loads real weights synchronously and does not expose
        # a meta-device fast-init path) or raises during construction
        # -- already caught and translated to ModelLoadError by
        # BaseGenomicModel.load()'s _factory() wrapper -- so there is
        # no separate partial-load state left to detect here.
        return None

    def _report_precision(self) -> str:
        return f"{self._dtype} ({CONFIG.models.EVO2_VARIANT}, StripedHyena-2)"

    def reported_max_length(self):
        return CONFIG.models.EVO2_MAX_SAFE_TOKENS

    def unload(self) -> None:
        # BaseGenomicModel.unload() clears self.model/self.tokenizer,
        # but this class also holds the actual Evo2 wrapper on a
        # separate attribute (see _load_impl) -- drop that reference
        # too, or its GPU-resident weights would outlive the "unload".
        self._evo2 = None
        super().unload()

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        # Evo 2's own published context window (up to ~1,000,000bp for
        # the 7B model) is far larger than any window GEPER ever
        # builds, so -- unlike Nucleotide Transformer's overlapping-
        # chunk strategy, which existed specifically to work around a
        # real 2048-token model limit smaller than GEPER's windows --
        # a simple defensive truncate-with-warning (mirroring
        # HyenaDNA's own approach) is the right-sized safeguard here,
        # not a chunking scheme solving a problem that doesn't exist.
        max_len = CONFIG.models.EVO2_MAX_SAFE_TOKENS
        truncated = False
        if len(sequence) > max_len:
            self.logger.warning(
                f"Sequence length {len(sequence)} exceeds Evo2's configured "
                f"safety ceiling of {max_len}; truncating. (This ceiling is "
                "a GEPER-side safety limit, not a limit of Evo 2 itself.)"
            )
            sequence = sequence[:max_len]
            truncated = True

        input_ids = torch.tensor(
            self.tokenizer.tokenize(sequence), dtype=torch.int
        ).unsqueeze(0).to(self.device)

        layer_name = CONFIG.models.EVO2_EMBEDDING_LAYER
        _, embeddings = self._evo2(
            input_ids, return_embeddings=True, layer_names=[layer_name]
        )
        layer_embedding = embeddings[layer_name].float()
        pooled = layer_embedding.mean(dim=1)

        return {
            "embedding_mean": pooled.squeeze(0).cpu().tolist(),
            "embedding_dim": layer_embedding.shape[-1],
            "num_tokens": int(input_ids.shape[-1]),
            "truncated": truncated,
            "embedding_layer": layer_name,
            "dtype": str(self._dtype),
        }
