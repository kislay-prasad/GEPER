"""
Abstract base class for every pretrained model wrapper in GEPER.

Design goals (per project requirements):
  - Every model has its own class.
  - Every model is independent (no cross-imports between model classes).
  - Each model is loaded only once and cached (via ModelCache).
  - GPU is auto-detected with CPU fallback.
  - Adding a new model later = subclass BaseGenomicModel + implement
    3 methods, then add one line to `models/__init__.py::MODEL_REGISTRY`.

CORRECTED 2026-08-22 -- the last bullet used to end "No other file
needs to change." That was TRUE WHEN WRITTEN, for the original
five-model set (hyenadna/evo2/rna_fm/esm2/alphamissense). It is not
true in general: this is only ONE of two contracts GEPER now uses to
add a model. Five models (Enformer, Borzoi, SpliceFormer, SpliceBERT,
SPiP) don't subclass BaseGenomicModel at all -- they subclass
`PluginModel` (`pipeline/models/base.py`) and register through
`pipeline/models/pending_plugins.py::build_default_registry()` /
`pipeline/models/manager.py::ModelManager`, plus a `config.py`
`ENABLE_<MODEL>` flag. See `models/__init__.py`'s module docstring for
the full explanation of both contracts and when each applies -- not
repeated here to avoid the two copies drifting against each other.
"""

import abc
import time
from typing import Any, Dict

import torch

from utils.device_utils import get_device
from utils.exceptions import ModelInferenceError, ModelLoadError
from utils.logger import get_logger
from utils.model_cache import ModelCache


class BaseGenomicModel(abc.ABC):
    """
    Common lifecycle for all pretrained genomic foundation models.

    Subclasses must implement:
        cache_key()      -> unique string identifying this model in ModelCache
        _load_impl()     -> loads tokenizer/model, returns them
        _infer_impl(...) -> runs a forward pass and returns structured output

    The public entry points `load()` and `predict()` wrap these with
    logging, timing, device placement, and exception translation so
    subclasses only implement model-specific logic.
    """

    def __init__(self):
        self.logger = get_logger(self.__class__.__module__)
        self.device: torch.device = get_device()
        self.tokenizer: Any = None
        self.model: Any = None
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Availability (optional override)
    # ------------------------------------------------------------------
    @classmethod
    def is_available(cls) -> bool:
        """
        Whether this model can even be attempted in the current
        environment, without actually downloading/loading weights.
        Defaults to True (HF Hub models: availability just depends on
        network + disk, which _load_impl already handles/reports).
        Override for models with an additional local dependency (e.g.
        HyenaDNA's standalone_hyenadna package) so the orchestrator can
        skip routing to them entirely -- once, with one clear warning
        -- instead of re-attempting and re-failing for every variant.
        """
        return True

    @classmethod
    def unavailability_reason(cls) -> str:
        """
        Short, human-readable reason this model is unavailable, shown
        in the startup validation table and run summary when
        `is_available()` returns False. Defaults to the historically
        only reason any model in GEPER has been unavailable (a missing
        optional package, e.g. HyenaDNA's `standalone_hyenadna`).
        Override this alongside `is_available()` when a model can be
        unavailable for a different, more specific reason (e.g. Evo 2
        being present-but-unsupported on a given GPU architecture) so
        the person running the pipeline sees *why*, not just *that*.

        A model whose unavailability can mean "we never checked" must
        override this: the default cannot distinguish a package that was
        checked and found missing from one nothing ever looked for. See
        `utils/auto_install.py::PackageCheckStatus`.
        """
        return "not installed"

    # ------------------------------------------------------------------
    # Abstract contract
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def cache_key(self) -> str:
        """Unique key used by ModelCache (e.g. 'hyenadna')."""

    @abc.abstractmethod
    def _load_impl(self):
        """
        Load tokenizer + model weights and move the model to
        `self.device`. Must set `self.tokenizer` and `self.model`.
        """

    @abc.abstractmethod
    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        """Run inference on a single sequence and return a result dict."""

    # ------------------------------------------------------------------
    # Shared lifecycle
    # ------------------------------------------------------------------
    def load(self) -> "BaseGenomicModel":
        """Idempotently load the model, using the shared model cache."""
        if self._loaded:
            return self

        def _factory():
            start = time.time()
            try:
                self._load_impl()
                # Root-cause safety net (not a patch over the symptom):
                # regardless of *why* a parameter might have been left
                # unmaterialized -- a meta-device fast-init path, a
                # checkpoint/architecture key mismatch, accelerate's
                # init_empty_weights(), device_map="auto", etc -- a
                # model must never be handed to inference with any
                # parameter still on the 'meta' device. `.to(device)`
                # is a silent no-op on an already-meta tensor (it has
                # no storage to move), so the only safe contract is to
                # verify materialization explicitly right after load
                # and fail loudly, here, before a single variant is
                # ever processed -- not three hours into a run on the
                # first forward pass that happens to touch that tensor.
                self._verify_materialized()
            except Exception as exc:  # noqa: BLE001 - translate all load errors
                raise ModelLoadError(f"Failed to load model '{self.cache_key()}': {exc}") from exc
            elapsed = time.time() - start
            self.logger.info(
                f"Loaded '{self.cache_key()}' on {self.device} ({self._report_precision()}) in {elapsed:.1f}s."
            )
            self._loaded = True
            return self

        cached = ModelCache.get_or_create(self.cache_key(), _factory)
        # get_or_create returns 'self' either way (freshly loaded or
        # the cached instance produced by a prior call to this class).
        return cached

    def _verify_materialized(self) -> None:
        """
        Fail fast if any parameter or buffer of `self.model` is still on
        the 'meta' device after `_load_impl()` returns. This is the
        direct, root-cause check for the class of bug behind "Tensor on
        device meta is not on the expected device cpu!": that error
        happens the moment a meta tensor is used in a real op (e.g. a
        forward pass), which can be arbitrarily far downstream of the
        actual defect (a checkpoint/architecture mismatch under a
        meta-device fast-init path). Catching it here, immediately
        after load and before any inference, turns a confusing runtime
        crash mid-pipeline into an immediate, precisely-named
        ModelLoadError.
        """
        if self.model is None:
            return
        meta_params = [name for name, tensor in self.model.named_parameters() if tensor.device.type == "meta"]
        meta_buffers = [name for name, tensor in self.model.named_buffers() if tensor.device.type == "meta"]
        if meta_params or meta_buffers:
            raise ModelLoadError(
                f"'{self.cache_key()}' has unmaterialized (meta-device) "
                f"tensors after loading -- parameters: {meta_params or 'none'}; "
                f"buffers: {meta_buffers or 'none'}. This means the "
                "checkpoint's state_dict did not cover every parameter the "
                "live model architecture defines while a meta-device "
                "fast-init path (low_cpu_mem_usage / accelerate / "
                "device_map) was active, so those tensors were never given "
                "real storage; `.to(device)` cannot fix this after the "
                "fact. See the loading code for this model for the "
                "specific fix (e.g. disabling the fast-init path so the "
                "model is built with real storage from construction)."
            )

    def _report_precision(self) -> str:
        """Best-effort dtype string for logging / the startup report."""
        try:
            return str(next(self.model.parameters()).dtype)
        except (StopIteration, AttributeError):
            return "n/a"

    def reported_max_length(self):
        """
        The effective maximum input length this model will accept,
        for the startup validation report. Subclasses that enforce a
        length (all of them, in practice) should override this;
        default is None (unknown/unbounded) rather than a guess.
        """
        return None

    def predict(self, sequence: str, **kwargs) -> Dict[str, Any]:
        """Public inference entry point with error handling + timing."""
        if not self._loaded:
            self.load()
        if not sequence:
            raise ModelInferenceError(f"'{self.cache_key()}' received an empty sequence.")
        start = time.time()
        try:
            # inference_mode() is strictly stronger than no_grad(): it
            # also disables the autograd version-counter bookkeeping
            # PyTorch would otherwise do on every tensor, which is pure
            # overhead here since none of these forward passes are ever
            # followed by a backward pass.
            with torch.inference_mode():
                result = self._infer_impl(sequence, **kwargs)
        except RuntimeError as exc:
            # CUDA errors (e.g. a device-side assert from an oversized
            # input that slipped past a model's own length guard) leave
            # the CUDA context in an unreliable state for *this*
            # process. We can't fully undo that, but we can: (1) stop
            # pretending this model instance is still usable so the
            # next call re-loads fresh weights rather than reusing
            # possibly-corrupted state, (2) release whatever memory we
            # can, and (3) translate to ModelInferenceError so the
            # per-variant/per-model isolation in the orchestrator keeps
            # every other model and every other variant running.
            msg = str(exc)
            if "CUDA" in msg or "device-side assert" in msg:
                self.logger.error(
                    f"CUDA error during '{self.cache_key()}' inference on a "
                    f"sequence of length {len(sequence)}: {msg}. Clearing "
                    "CUDA cache and unloading this model; if CUDA errors "
                    "persist on the next call, the CUDA context itself is "
                    "likely corrupted and the Python process/Colab runtime "
                    "needs to be restarted."
                )
                self.unload()
            raise ModelInferenceError(
                f"Inference failed for '{self.cache_key()}' on sequence of length {len(sequence)}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ModelInferenceError(
                f"Inference failed for '{self.cache_key()}' on sequence of length {len(sequence)}: {exc}"
            ) from exc
        elapsed = time.time() - start
        result.setdefault("meta", {})
        result["meta"].update(
            {
                "model": self.cache_key(),
                "device": str(self.device),
                "sequence_length": len(sequence),
                "inference_seconds": round(elapsed, 4),
            }
        )
        return result

    def unload(self) -> None:
        """Release model weights (e.g. to free GPU memory between batches)."""
        self.model = None
        self.tokenizer = None
        self._loaded = False
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.logger.info(f"Unloaded '{self.cache_key()}'.")

    def _resolve_safe_max_length(self, fallback: int) -> int:
        """
        Determine a safe maximum token length for this model's
        tokenizer, so no model ever hands more tokens to itself (and
        therefore the GPU) than it can safely process -- the root
        cause behind both the Nucleotide Transformer overflow and the
        CUDA device-side assert that followed it.

        Prefers the tokenizer's own declared `model_max_length`.
        HuggingFace tokenizers without an explicit limit in their
        config expose a sentinel (~1e30) rather than a real bound,
        which is why that value is sanity-checked before being
        trusted; `fallback` (a documented constant in config.py, not a
        value invented here) is used when the tokenizer's number isn't
        usable.
        """
        tok_max = getattr(self.tokenizer, "model_max_length", None)
        if isinstance(tok_max, int) and 0 < tok_max < 1_000_000:
            return tok_max
        return fallback

    @staticmethod
    def mean_pool(embeddings: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        """
        Attention-mask-aware mean pooling over the sequence/token
        dimension. Shared utility so every embedding-based model
        (HyenaDNA, NT, RNA-FM, ESM-2) reduces token-level
        hidden states to a single fixed-size vector the same way.
        """
        mask = attention_mask.unsqueeze(-1).expand(embeddings.size()).float()
        summed = torch.sum(embeddings * mask, dim=1)
        counts = torch.clamp(mask.sum(dim=1), min=1e-9)
        return summed / counts
