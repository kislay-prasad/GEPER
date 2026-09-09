"""
HyenaDNA wrapper.

HyenaDNA uses long-convolution (not attention) layers, allowing it to
handle sequence lengths up to 450,000 bp with sub-quadratic compute.
GEPER routes to this model only when the required sequence context is
long (structural variants, large regulatory windows, repeat regions)
per config.RoutingConfig.HYENADNA_MIN_LEN.

HyenaDNA is not published as a standard AutoModel-compatible
HuggingFace repo; the official usage pattern loads a checkpoint
directory via a `standalone_hyenadna.py` file (cloned from
HazyResearch/hyena-dna) plus a character-level tokenizer, both
imported lazily inside `_load_impl` so importing this module does not
require the HyenaDNA source unless HyenaDNA is actually selected by
the router, keeping GEPER's other models fully independent of it.

IMPORTANT: this module does NOT import the official repo's
`huggingface.py`. That file's `HyenaDNAPreTrainedModel.from_pretrained`
helper is what the model card documents, but the file also calls
`inference_single()` unconditionally at module level -- not guarded by
`if __name__ == "__main__"` -- hardcoded to a different model size
than GEPER uses (verified against the actual published source, not
assumed from the model card). A bare `import huggingface` would
silently download the wrong checkpoint and run inference as a side
effect of importing this module. Instead, `_load_hyenadna_checkpoint`
below reimplements just the checkpoint-loading logic from that
function, scoped to GEPER's configured model and without the
unconditional call.
"""

import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Sequence

import torch

from config import CONFIG
from models.base_model import BaseGenomicModel
from utils.exceptions import ModelLoadError
from utils.logger import get_logger

logger = get_logger(__name__)


def is_hyenadna_installed() -> bool:
    """
    Cheap, import-free check for whether the standalone_hyenadna
    package is on PYTHONPATH, so the orchestrator/router can decide
    not to route to HyenaDNA at all -- rather than repeatedly
    attempting and failing to load it for every long-context variant
    in a run.
    """
    return importlib.util.find_spec("standalone_hyenadna") is not None


# Set once auto-install has been attempted this process, so a broken
# environment (no git, no network) fails fast with one clear error
# instead of re-attempting a doomed git clone on every subsequent
# is_available()/_load_impl() call (e.g. once per variant that routes
# here). None = not attempted yet; True/False = outcome of that attempt.
_AUTO_INSTALL_RESULT = None

# Pinned commit for HazyResearch/hyena-dna (the SOURCE repo, cloned for
# `standalone_hyenadna.py`/the character tokenizer -- see
# `install_hyenadna_colab` below). No tags or releases exist upstream
# (verified live via `git ls-remote --tags`, empty, 2026-08-27), so this
# pins to the exact commit that was `main`'s tip at verification time --
# the closest reproducibility guarantee available without one. An
# unpinned `--depth 1` clone of a moving branch means every fresh
# environment can silently get a different `standalone_hyenadna.py`
# than the one this integration was tested against.
_HYENADNA_REPO_COMMIT = "d553021b483b82980aa4b868b37ec2d4332e198a"

# Where the vendored HazyResearch/hyena-dna checkout lives, RESOLVED FROM
# THIS FILE rather than from the process's working directory.
#
# This was `"./hyena-dna"` -- a bare CWD-relative path -- and that is a
# defect rather than a style question. The Docker image ships the source at
# /app/geper/hyena-dna and runs with WORKDIR=/app, so `./hyena-dna` pointed
# at a directory that does not exist, the loader could not see the checkout
# already sitting next to it, and under `docker run --network none` it fell
# through to `git clone` and failed with
#   fatal: unable to access 'https://github.com/HazyResearch/hyena-dna.git/':
#   Could not resolve host: github.com
# The image was only offline-capable because an `ENV PYTHONPATH` entry made
# `find_spec("standalone_hyenadna")` succeed before this path was consulted
# -- i.e. offline BY ACCIDENT, and only when launched from the right
# directory. Resolving from `__file__` makes it a property of the checkout
# instead of a property of whoever invoked the process.
#
# This file is geper/models/hyenadna.py, so two dirnames up is the `geper/`
# package root and the vendored checkout is its sibling of `models/`. That
# is the SAME location the Dockerfile already installs to; the path is not
# being moved, it is being addressed correctly.
#
# It is also the clone DESTINATION when the source is genuinely absent, and
# that fixes a second instance of the same bug: a clone into the CWD is
# invisible to the next process started from anywhere else, so the "already
# cloned by a previous run" branch below could never fire and every run
# re-cloned. Now the clone lands where the next process will look.
_VENDORED_REPO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hyena-dna")

# Pinned HuggingFace revision (commit SHA) for each supported
# HYENADNA_MODEL_NAME's checkpoint repo (huggingface.co/LongSafari/
# {model_name}), verified live via `git ls-remote <repo-url> HEAD`,
# 2026-08-27. See `_load_hyenadna_checkpoint` below for why this is
# keyed by model_name rather than a single constant.
_HYENADNA_CHECKPOINT_REVISIONS = {
    "hyenadna-medium-450k-seqlen": "820fe013ec58c0f87f8caba21af0c48fdc8d0ddd",
}


def ensure_hyenadna_available(checkpoint_dir: str = None) -> bool:
    """
    Idempotent, automatic setup entry point used by `is_available()`
    and `_load_impl()` so the pipeline discovers/initializes HyenaDNA
    on its own -- a fresh checkout needs nothing beyond
    `python main.py`, no manual `install_hyenadna_colab()` call first.

    Returns immediately (no subprocess) if `standalone_hyenadna` is
    already importable. Otherwise attempts the same one-time clone
    `install_hyenadna_colab()` performs, exactly once per process
    (result cached in `_AUTO_INSTALL_RESULT`), and returns whether
    HyenaDNA is importable afterward.
    """
    global _AUTO_INSTALL_RESULT

    if is_hyenadna_installed():
        return True

    if _AUTO_INSTALL_RESULT is not None:
        # Already tried once this process and it didn't work (e.g. no
        # network/git access) -- don't retry per-variant.
        return _AUTO_INSTALL_RESULT

    logger.info(
        "HyenaDNA ('standalone_hyenadna') is not yet installed; "
        "installing it automatically now (one-time git clone of "
        "HazyResearch/hyena-dna) so the pipeline can proceed without a "
        "manual setup step."
    )
    _AUTO_INSTALL_RESULT = install_hyenadna_colab(checkpoint_dir)
    return _AUTO_INSTALL_RESULT


def install_hyenadna_colab(checkpoint_dir: str = None) -> bool:
    """
    Installation helper that clones HazyResearch/hyena-dna and adds it
    to sys.path so `standalone_hyenadna` (HyenaDNAModel,
    CharacterTokenizer) becomes importable. Checkpoint download
    happens lazily inside `_load_hyenadna_checkpoint` the first time
    HyenaDNA is actually loaded, not here -- this function only makes
    the source importable.

    Called automatically by `ensure_hyenadna_available()` (itself
    called from `is_available()`/`_load_impl()`) the first time
    HyenaDNA is needed, so users do not need to call this directly.
    Kept public for anyone who wants to pre-install HyenaDNA
    explicitly (e.g. in a Colab setup cell, to control exactly when
    the clone happens) rather than relying on the automatic trigger.
    """
    checkpoint_dir = checkpoint_dir or CONFIG.models.HYENADNA_CHECKPOINT_DIR
    repo_dir = _VENDORED_REPO_DIR
    try:
        if not is_hyenadna_installed():
            if os.path.isdir(repo_dir):
                # Already cloned by a previous process/run (e.g. a
                # Colab reconnect) -- it's just not on *this* fresh
                # process's sys.path yet. Re-running `git clone` here
                # would fail with "destination path already exists",
                # so just make the existing checkout importable
                # instead of re-cloning it.
                logger.info(
                    f"Found existing '{repo_dir}' from a previous run; adding it to sys.path instead of re-cloning."
                )
            else:
                logger.info(f"Cloning HazyResearch/hyena-dna @ {_HYENADNA_REPO_COMMIT} for HyenaDNA support...")
                # Full clone, not --depth 1: checking out a specific
                # historical commit (there are no tags to shallow-clone
                # by) needs the commit's history to be present locally.
                subprocess.run(
                    ["git", "clone", "--quiet", "https://github.com/HazyResearch/hyena-dna.git", repo_dir],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", repo_dir, "checkout", "--quiet", _HYENADNA_REPO_COMMIT],
                    check=True,
                )
            if repo_dir not in sys.path:
                sys.path.append(repo_dir)
            importlib.invalidate_caches()
        os.makedirs(checkpoint_dir, exist_ok=True)
        installed = is_hyenadna_installed()
        if installed:
            logger.info(
                "HyenaDNA source is now importable. The configured "
                f"checkpoint ('{CONFIG.models.HYENADNA_MODEL_NAME}') will "
                f"be downloaded into '{checkpoint_dir}' automatically the "
                "first time HyenaDNA is loaded."
            )
        else:
            logger.error(
                "Cloned hyena-dna but 'standalone_hyenadna' still isn't "
                "importable; check the clone succeeded and is on sys.path."
            )
        return installed
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.error(f"Automatic HyenaDNA installation failed: {exc}")
        return False


def _build_character_tokenizer(model_max_length: int):
    """
    Returns a `CharacterTokenizer` (from the cloned standalone_hyenadna
    module) patched for compatibility with current `transformers`.

    Verified bug (reproduced against the real cloned source, current
    transformers): `CharacterTokenizer.__init__` calls
    `super().__init__(...)` (`PreTrainedTokenizer.__init__`) BEFORE
    setting `self._vocab_str_to_int`. Newer `transformers` versions
    call `self.get_vocab()` from inside that same `__init__` (via
    `_add_tokens`), which `CharacterTokenizer` never overrides in the
    first place (falls through to `PreTrainedTokenizer.get_vocab()`,
    which raises `NotImplementedError`) -- and even once `get_vocab()`
    is added, it's called before `_vocab_str_to_int` exists, raising
    `AttributeError`. Fixed here by building the vocab first and
    calling `PreTrainedTokenizer.__init__` directly, skipping
    `CharacterTokenizer.__init__`'s (broken, for this transformers
    version) ordering entirely.
    """
    from standalone_hyenadna import CharacterTokenizer
    from transformers.tokenization_utils import AddedToken, PreTrainedTokenizer

    class _PatchedCharacterTokenizer(CharacterTokenizer):
        def __init__(self, characters: Sequence[str], model_max_length: int, padding_side: str = "left", **kwargs):
            self.characters = characters
            self.model_max_length = model_max_length
            self._vocab_str_to_int = {
                "[CLS]": 0,
                "[SEP]": 1,
                "[BOS]": 2,
                "[MASK]": 3,
                "[PAD]": 4,
                "[RESERVED]": 5,
                "[UNK]": 6,
                **{ch: i + 7 for i, ch in enumerate(characters)},
            }
            self._vocab_int_to_str = {v: k for k, v in self._vocab_str_to_int.items()}

            bos_token = AddedToken("[BOS]", lstrip=False, rstrip=False)
            sep_token = AddedToken("[SEP]", lstrip=False, rstrip=False)
            cls_token = AddedToken("[CLS]", lstrip=False, rstrip=False)
            pad_token = AddedToken("[PAD]", lstrip=False, rstrip=False)
            unk_token = AddedToken("[UNK]", lstrip=False, rstrip=False)
            mask_token = AddedToken("[MASK]", lstrip=True, rstrip=False)

            PreTrainedTokenizer.__init__(
                self,
                bos_token=bos_token,
                eos_token=sep_token,
                sep_token=sep_token,
                cls_token=cls_token,
                pad_token=pad_token,
                mask_token=mask_token,
                unk_token=unk_token,
                add_prefix_space=False,
                model_max_length=model_max_length,
                padding_side=padding_side,
                **kwargs,
            )

        def get_vocab(self):
            return dict(self._vocab_str_to_int)

    return _PatchedCharacterTokenizer(
        characters=["A", "C", "G", "T", "N"],
        model_max_length=model_max_length,
    )


def _allow_lightning_checkpoint_globals() -> None:
    """
    Registers the classes the official HyenaDNA `weights.ckpt` contains
    on PyTorch's strict-unpickling allowlist, so it loads with
    `weights_only` left at PyTorch's own default. Idempotent -- safe to
    call on every load.

    WHAT TRIPS THE NEWER DEFAULT. PyTorch 2.6 flipped `torch.load`'s
    `weights_only` default from `False` to `True`. This checkpoint is a
    PyTorch Lightning one, so its non-tensor payload is a Lightning
    training envelope (`epoch`, `global_step`, `loops`, `callbacks`,
    `optimizer_states`, `hyper_parameters`, ...) wrapped around the
    `state_dict` this loader actually uses. PyTorch names one rejected
    class at a time:

        WeightsUnpickler error: Unsupported global: GLOBAL
        omegaconf.listconfig.ListConfig was not an allowed global by
        default.

    Iterating that message to exhaustion against the pinned checkpoint
    gives exactly the ten classes below, after which the file loads
    with `weights_only=True`. THIS SET WAS ESTABLISHED BY PROBE, NOT BY
    ANALOGY: RNA-FM's equivalent problem is a single `argparse.Namespace`
    (see rna_fm.py), and NOT ONE OF THESE TEN IS THAT CLASS -- a
    different producer puts different things in the file, so the set
    has to be measured per checkpoint rather than assumed.

    WHY ALLOWLISTING THESE IS NARROW. Five are builtins/typing
    (`list`, `dict`, `int`, `collections.defaultdict`, `typing.Any`);
    the other five are omegaconf's config containers, which is a
    configuration library, not a code carrier. And omegaconf is ALREADY
    a hard requirement for reading this file at all -- unpickling the
    embedded config imports it either way, which is exactly why
    requirements.txt pins it (see the `omegaconf==2.3.1` note there).
    So this permits a fixed, inspected list of data holders instead of
    `weights_only=False`, which permits ANY global in the file.

    WHAT IT STILL PERMITS, STATED RATHER THAN GLOSSED: allowlisting a
    class lets the unpickler construct it, so omegaconf's own
    `__setstate__`/constructor runs during the load. That is a real
    surface -- far smaller than arbitrary globals, but not zero.

    CAVEAT, ACCEPTED KNOWINGLY: `add_safe_globals` is process-global.
    Once called, any `torch.load` in this process will also accept
    these ten classes.

    SCOPE: this set is the set for the PINNED checkpoint revision this
    module downloads (see `_HYENADNA_CHECKPOINT_REVISIONS`). A different
    variant or a re-pinned revision could contain more, and the failure
    mode if so is a loud `UnpicklingError` naming the missing class --
    not a silent wrong answer.
    """
    try:
        from omegaconf.base import ContainerMetadata, Metadata
        from omegaconf.dictconfig import DictConfig
        from omegaconf.listconfig import ListConfig
        from omegaconf.nodes import AnyNode
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ModelLoadError(
            "Reading the HyenaDNA checkpoint requires the 'omegaconf' "
            "package (it is embedded in the checkpoint's Lightning "
            "hyper-parameters and is needed to deserialize it at all, "
            "with or without weights_only) -- install it with "
            "`pip install omegaconf==2.3.1`."
        ) from exc

    torch.serialization.add_safe_globals(
        [
            ListConfig,
            DictConfig,
            AnyNode,
            ContainerMetadata,
            Metadata,
            Any,
            list,
            dict,
            int,
            collections.defaultdict,
        ]
    )


def _load_hyenadna_checkpoint(path: str, model_name: str, device: str):
    """
    Reimplementation of HazyResearch/hyena-dna's
    `HyenaDNAPreTrainedModel.from_pretrained` (from `huggingface.py`),
    NOT an import of it -- see the module docstring for why. Downloads
    (if not already present locally) and loads the official pretrained
    checkpoint for `model_name` from the LongSafari HuggingFace org.
    """
    from standalone_hyenadna import HyenaDNAModel as _HyenaDNABackbone

    pretrained_dir = os.path.join(path, model_name)
    if not os.path.isdir(pretrained_dir):
        hf_url = f"https://huggingface.co/LongSafari/{model_name}"
        os.makedirs(path, exist_ok=True)
        # Pinned by model_name, not a single global constant: the
        # checkpoint lives in a model-name-specific HF repo
        # (huggingface.co/LongSafari/{model_name}), so a commit verified
        # against one variant's history has no meaning against another's.
        # CONFIG.models.HYENADNA_MODEL_NAME is a plain class attribute
        # today (not env-configurable, unlike HYENADNA_CHECKPOINT_DIR
        # right above it in config.py) -- but the moment it becomes one,
        # an unmapped variant should fail loudly here, not silently fall
        # back to an unpinned clone and defeat the point of pinning.
        revision = _HYENADNA_CHECKPOINT_REVISIONS.get(model_name)
        if revision is None:
            raise ModelLoadError(
                f"No pinned HuggingFace revision is recorded for HyenaDNA "
                f"checkpoint '{model_name}'. Verify the exact commit to use "
                f"(e.g. `git ls-remote {hf_url} HEAD`), add it to "
                "_HYENADNA_CHECKPOINT_REVISIONS in models/hyenadna.py, and "
                "retry -- refusing to download an unpinned checkpoint."
            )
        logger.info(f"Downloading HyenaDNA checkpoint '{model_name}' @ {revision} from {hf_url}...")
        try:
            subprocess.run(["git", "lfs", "install"], check=True, cwd=path)
            subprocess.run(["git", "clone", hf_url], check=True, cwd=path)
            subprocess.run(["git", "-C", pretrained_dir, "checkout", "--quiet", revision], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise ModelLoadError(
                f"Failed to download HyenaDNA checkpoint '{model_name}' @ "
                f"{revision} via git-lfs clone from {hf_url}: {exc}. "
                "git-lfs must be installed (apt-get install git-lfs) for "
                "this download."
            ) from exc

    config_path = os.path.join(pretrained_dir, "config.json")
    weights_path = os.path.join(pretrained_dir, "weights.ckpt")
    if not os.path.isfile(config_path) or not os.path.isfile(weights_path):
        raise ModelLoadError(
            f"HyenaDNA checkpoint directory '{pretrained_dir}' is missing "
            f"'config.json' or 'weights.ckpt' after download; the clone "
            "may have failed partway (check git-lfs is installed)."
        )

    with open(config_path) as f:
        hyena_config = json.load(f)

    scratch_model = _HyenaDNABackbone(**hyena_config, use_head=False, n_classes=2)
    # Strict unpickling stays ON; the ten data classes this official
    # checkpoint contains are allowlisted instead of disabling it (see
    # `_allow_lightning_checkpoint_globals` for the full reasoning).
    _allow_lightning_checkpoint_globals()
    loaded_ckpt = torch.load(
        weights_path,
        map_location=torch.device(device),
    )

    # "State dict surgery": the checkpoint's keys are prefixed with
    # 'model.' relative to the scratch backbone's own keys (and, if the
    # checkpoint was trained with gradient checkpointing, need an extra
    # '.layer' segment injected into mixer/mlp submodule names). This
    # mirrors the official `load_weights`/`inject_substring` helpers
    # in huggingface.py exactly, but raises a specific ModelLoadError
    # (naming the missing key) instead of a bare `except: raise
    # Exception(...)` on any mismatch.
    checkpointing = bool(hyena_config.get("checkpoint_mixer", False))
    scratch_dict = scratch_model.state_dict()
    pretrained_dict = loaded_ckpt["state_dict"]
    for key in scratch_dict:
        if "backbone" not in key:
            continue
        key_loaded = "model." + key
        if checkpointing:
            key_loaded = re.sub(r"\.mixer", ".mixer.layer", key_loaded)
            key_loaded = re.sub(r"\.mlp", ".mlp.layer", key_loaded)
        if key_loaded not in pretrained_dict:
            raise ModelLoadError(
                f"HyenaDNA checkpoint key mismatch: expected checkpoint "
                f"key '{key_loaded}' (for scratch model key '{key}') but "
                f"it was not found in weights.ckpt's state_dict. The "
                "checkpoint format may have changed upstream."
            )
        scratch_dict[key] = pretrained_dict[key_loaded]

    scratch_model.load_state_dict(scratch_dict)
    return scratch_model


class HyenaDNAModel(BaseGenomicModel):
    """Embeds long DNA sequences using HyenaDNA for long-range context analysis."""

    def cache_key(self) -> str:
        return "hyenadna"

    @classmethod
    def is_available(cls) -> bool:
        # Auto-installs on first call (see ensure_hyenadna_available)
        # rather than just reporting the current state, so the
        # orchestrator's up-front availability probe -- which decides
        # whether HyenaDNA is even attempted this run -- reflects
        # reality after setup, not before it.
        return ensure_hyenadna_available()

    def _load_impl(self):
        if not ensure_hyenadna_available():
            raise ModelLoadError(
                "HyenaDNA requires the official 'standalone_hyenadna' module, "
                "and automatic installation (git clone of "
                "HazyResearch/hyena-dna) did not succeed in this environment "
                "-- check that 'git' is installed and that this environment "
                "has network access to github.com. You can also install it "
                "yourself with models.hyenadna.install_hyenadna_colab(), or "
                "by cloning https://github.com/HazyResearch/hyena-dna and "
                "placing it on PYTHONPATH."
            )

        self.tokenizer = _build_character_tokenizer(CONFIG.models.HYENADNA_MAX_LENGTH)
        self.model = _load_hyenadna_checkpoint(
            CONFIG.models.HYENADNA_CHECKPOINT_DIR,
            CONFIG.models.HYENADNA_MODEL_NAME,
            device=str(self.device),
        )
        self.model.to(self.device)
        self.model.eval()

    def reported_max_length(self):
        return CONFIG.models.HYENADNA_MAX_LENGTH

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        max_len = CONFIG.models.HYENADNA_MAX_LENGTH
        if len(sequence) > max_len:
            self.logger.warning(
                f"Sequence length {len(sequence)} exceeds HyenaDNA max {max_len}; truncating to model limit."
            )
            sequence = sequence[:max_len]

        encoded = self.tokenizer(
            sequence,
            return_tensors="pt",
            padding=False,
            truncation=True,
            max_length=max_len,
        )
        input_ids = encoded["input_ids"].to(self.device)

        outputs = self.model(input_ids)
        hidden_states = outputs[0] if isinstance(outputs, tuple) else outputs

        pooled = hidden_states.mean(dim=1)

        return {
            "embedding_mean": pooled.squeeze(0).cpu().tolist(),
            "embedding_dim": hidden_states.shape[-1],
            "num_tokens": int(hidden_states.shape[1]),
            "truncated": len(sequence) >= max_len,
        }
