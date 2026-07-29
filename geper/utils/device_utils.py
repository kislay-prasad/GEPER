"""
Device detection utilities.

Every model class calls `get_device()` to decide where to place its
weights. Detection is centralized here so the fallback logic (CUDA ->
Apple MPS -> CPU) and any future changes (e.g. multi-GPU sharding) only
need to be implemented once.
"""

from functools import lru_cache

import torch

from utils.logger import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_device() -> torch.device:
    """
    Detect the best available compute device.

    Resolution order:
        1. CUDA GPU (NVIDIA, including Colab T4/A100/L4)
        2. Apple Metal (MPS) -- useful for local Mac development
        3. CPU fallback

    The result is cached (per-process) since device availability does
    not change during a run.
    """
    if torch.cuda.is_available():
        device = torch.device("cuda")
        name = torch.cuda.get_device_name(0)
        mem_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        logger.info(f"GPU detected: {name} ({mem_gb:.1f} GB VRAM). Using CUDA.")
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        device = torch.device("mps")
        logger.info("Apple MPS backend detected. Using MPS.")
    else:
        device = torch.device("cpu")
        logger.warning(
            "No GPU detected. Falling back to CPU. Large models "
            "(ESM-2 650M) will be slow or may exceed available RAM on "
            "CPU; Evo 2 has no practical CPU path at all and will be "
            "skipped entirely in this environment."
        )
    return device


@lru_cache(maxsize=1)
def log_environment_versions() -> None:
    """
    Log the installed torch / transformers / accelerate versions once
    per process at startup, before any model is loaded.

    This exists because the meta-device / "Tensor on device meta is
    not on the expected device cpu!" class of bug is fundamentally a
    *version-compatibility* issue, not a logic bug in GEPER's own
    code: it surfaces when a `trust_remote_code=True` model's custom
    architecture (frozen at whatever `transformers` API existed when
    the model was published) is loaded under a newer `transformers` /
    `accelerate` combination whose default fast-init behavior has
    changed. Recording the exact versions in every run's log is what
    makes that kind of failure reproducible and diagnosable later,
    instead of "it worked yesterday."
    """
    try:
        import accelerate
        import transformers

        logger.info(
            f"Environment: torch={torch.__version__}, "
            f"transformers={transformers.__version__}, "
            f"accelerate={accelerate.__version__}, "
            f"cuda_available={torch.cuda.is_available()}."
        )
    except ImportError as exc:  # pragma: no cover - defensive only
        logger.warning(f"Could not resolve full environment version info: {exc}")


def get_cuda_compute_capability():
    """
    Return the (major, minor) CUDA compute capability of GPU 0, or
    None if no CUDA GPU is present.

    This exists specifically because "a CUDA GPU is present" (what
    `torch.cuda.is_available()` answers) and "this GPU's architecture
    is new enough for a given kernel library" are different questions.
    FlashAttention-2 -- a hard dependency of Arc Institute's `evo2`
    package via its `vtx`/StripedHyena-2 attention path -- only ships
    compiled kernels for Ampere/Ada/Hopper (compute capability >= 8.0);
    Turing GPUs (compute capability 7.5 -- this includes the Tesla T4)
    are not covered, and there is no official Arc Institute-supported
    fallback attention path for evo2 on Turing (see models/evo2.py for
    the full citation trail). Centralizing the capability check here
    means any current or future model with the same kind of
    architecture-specific (not just "is there a GPU") requirement can
    reuse it instead of re-deriving it.

    Deliberately NOT `@lru_cache`d (unlike `get_device()` above): the
    underlying `torch.cuda.get_device_capability` call is cheap (a
    single driver query, not a multi-GB weight load), and this keeps
    the function trivially mockable per-test rather than sticky for
    the rest of the process the first time it's called.
    """
    if not torch.cuda.is_available():
        return None
    return torch.cuda.get_device_capability(0)


def gpu_memory_summary() -> str:
    """Return a short human-readable string of current GPU memory usage."""
    if not torch.cuda.is_available():
        return "No GPU available."
    allocated = torch.cuda.memory_allocated() / (1024 ** 3)
    reserved = torch.cuda.memory_reserved() / (1024 ** 3)
    return f"GPU memory -> allocated: {allocated:.2f} GB, reserved: {reserved:.2f} GB"
