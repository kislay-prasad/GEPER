"""
Loader for the official Spliceformer model + one pretrained checkpoint.

Distinct from `pipeline.models.cache.WeightCache` (the generic
on-disk-weight-file cache every plugin shares) the same way
`pipeline/models/mmsplice/loader.py` is distinct from
`pipeline/models/mmsplice/cache.py`: this module is about
*constructing the model and getting weights into it*; `cache.py`
(reused here, not reimplemented) is about *where the downloaded
weight file lives on disk*.
"""

from pathlib import Path

import torch

from utils.logger import get_logger

logger = get_logger(__name__)

# Pinned to the v1.0.0 GitHub release tag (== the Zenodo-archived
# release, DOI 10.5281/zenodo.14019451) rather than "main", so the
# exact source/weights GEPER downloads never silently change
# underneath a running deployment -- the same rationale
# EnformerPlugin/BorzoiPlugin already apply by pinning a specific
# HuggingFace repo id rather than "latest".
DEFAULT_SOURCE_REF = "v1.0.0"

# One of the ten "transformer_encoder_40k_171022_*" replicate
# checkpoints shipped in Results/PyTorch_Models/ in the official repo
# (replicate 0). The "40k" in the filename is the encoder's CL_max
# (flanking context, 40,000nt) -- NOT the same number as the "45k" in
# "Spliceformer-45k" (SL + CL_max = 5,000 + 40,000 = 45,000, the total
# input window; see TOTAL_INPUT_LENGTH below). Verified directly
# against the actual file listing of the pinned v1.0.0 tag -- no
# "*_45k_*"-named checkpoint exists anywhere in the repository; using
# that name 404s against raw.githubusercontent.com. The paper's own
# headline benchmarks average all ten replicate seeds (Methods, "Model
# training": "The model weights were randomly initialized ten
# times and trained... The reported results... are the average
# predictions of ten models."). GEPER's plugin runs a single replicate
# by default -- consistent with EnformerPlugin/BorzoiPlugin, which
# also each run one model instance rather than a multi-seed ensemble.
# Documented, not silently understated: see
# SpliceFormerPlugin.metadata().license_notes.
DEFAULT_CHECKPOINT = "transformer_encoder_40k_171022_0"

_RAW_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/benniatli/Spliceformer/{ref}/"
    "Results/PyTorch_Models/{checkpoint}"
)

# From the official repo's own inference/delta-scoring notebook
# (Code/get_clinvar_delta_for_transformer.ipynb): SL=5000, CL_max=40000
# -- total input length 45,000 ("Spliceformer-45k"). CL_max is the
# encoder's calibrated flanking context -- the pretrained weights
# below were trained with exactly this value, it is not a free
# parameter here -- SL is the number of central positions the model
# then scores per forward pass.
CL_MAX = 40_000
SL = 5_000
TOTAL_INPUT_LENGTH = SL + CL_MAX  # 45,000

# Model hyperparameters, exactly as used to instantiate SpliceFormer
# for inference in the official repo's own notebook (the cell that
# loads `transformer_encoder_...` checkpoints):
#   SpliceFormer(CL_max, bn_momentum=0.01, depth=4, heads=4,
#                n_transformer_blocks=2, determenistic=True)
# n_channels/dim_head/mlp_dim/maxSeqLength are left at model.py's own
# defaults (32/32/512/512) since the notebook doesn't override them
# either; depth=4 * n_transformer_blocks=2 = 8 total transformer
# encoder layers with 4 heads each, matching the paper's Methods
# description ("eight transformer encoders with four heads").
# `determenistic=True` is the documented test-time behavior (paper
# Methods: "during test time, we simply select the acceptors and
# donors with the largest logits").
MODEL_KWARGS = dict(
    bn_momentum=0.01,
    depth=4,
    heads=4,
    n_transformer_blocks=2,
    determenistic=True,
)


def build_model():
    """
    Instantiate the official, untrained `SpliceFormer` architecture
    (call `load_checkpoint_into` afterwards to load pretrained
    weights). The vendored module is imported here, not at this
    file's top level, so importing `spliceformer_plugin.py` (and
    therefore this module) never itself requires `einops`/a real
    `torch` install -- only actually building the model does. This
    mirrors how EnformerPlugin/BorzoiPlugin defer
    `import enformer_pytorch` / `import borzoi_pytorch` to inside
    `_load_impl`/`_infer_impl` rather than their own module top level.
    """
    from pipeline.models.spliceformer.vendor.model import SpliceFormer

    return SpliceFormer(CL_MAX, **MODEL_KWARGS)


def checkpoint_url(ref: str = DEFAULT_SOURCE_REF, checkpoint: str = DEFAULT_CHECKPOINT) -> str:
    return _RAW_URL_TEMPLATE.format(ref=ref, checkpoint=checkpoint)


def download_checkpoint(
    dest_path: Path, ref: str = DEFAULT_SOURCE_REF, checkpoint: str = DEFAULT_CHECKPOINT
) -> Path:
    """
    Downloads one official pretrained checkpoint file (a raw
    `torch.save`d state_dict -- see `load_checkpoint_into` below for
    the matching load convention) from the pinned GitHub ref into
    `dest_path`, streaming so the download (tens of MB) is never held
    fully in memory. Uses `requests`, already an unconditional GEPER
    dependency (see requirements.txt), rather than adding a new one.
    Writes to a `.part` sibling first and atomically renames on
    success, so a failed/interrupted download never leaves a
    corrupt file at `dest_path` for `WeightCache`/`is_available`-style
    callers to mistake for a good one.
    """
    import requests

    url = checkpoint_url(ref, checkpoint)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    logger.info(f"Downloading SpliceFormer checkpoint '{checkpoint}' from {url} ...")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp_path.replace(dest_path)
    logger.info(f"SpliceFormer checkpoint saved to {dest_path}.")
    return dest_path


def load_checkpoint_into(model, checkpoint_path: Path, device) -> None:
    """
    Loads a downloaded checkpoint's state_dict into `model`.

    The official repo's own notebook loads checkpoints with
    `model.load_state_dict(torch.load(path))` directly, but only after
    conditionally wrapping the target model in `nn.DataParallel` first
    (`if torch.cuda.device_count() > 1: model = nn.DataParallel(model)`
    -- see Code/get_clinvar_delta_for_transformer.ipynb). The published
    checkpoints were saved from a `DataParallel`-wrapped model (every
    key is prefixed `module.`, confirmed by inspecting the actual
    downloaded state_dict), so a direct `load_state_dict` against a
    plain, unwrapped `model` -- what `build_model()` returns, since
    GEPER's inference path is single-process/single-device and never
    wraps in `DataParallel` -- raises a "Missing key(s)"/"Unexpected
    key(s)" `RuntimeError` for every parameter. Stripping the `module.`
    prefix (the standard, well-established fix for loading a
    DataParallel-trained checkpoint into a non-DataParallel model --
    `DataParallel` only adds that one attribute-name layer around the
    exact same submodules) restores the same effective weights the
    notebook's own multi-GPU path would load, without requiring GEPER
    to actually wrap/unwrap a `DataParallel` module for a single
    device.
    """
    state_dict = torch.load(str(checkpoint_path), map_location=device)
    if all(key.startswith("module.") for key in state_dict):
        state_dict = {key[len("module."):]: value for key, value in state_dict.items()}
    model.load_state_dict(state_dict)
