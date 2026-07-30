"""
Loader for the official SpliceBERT checkpoint.

Distinct from `pipeline.models.cache.WeightCache` (the generic
on-disk-weight-file cache every plugin shares) the same way
`pipeline/models/spliceformer/loader.py` is distinct from it: this
module is about *fetching the official archive and getting one
checkpoint's files onto disk in a `transformers`-loadable layout*;
`cache.py` (reused here, not reimplemented) is about *where the
downloaded files live on disk*.
"""

import shutil
import tarfile
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# SpliceBERT's official weights are published ONLY on Zenodo (DOI
# 10.5281/zenodo.7995778) -- there is no GitHub release asset the way
# SpliceFormer's checkpoints are (see pipeline/models/spliceformer/
# loader.py); the repo's own `download.sh` fetches this exact URL.
# Pinned to a specific Zenodo record id (not "latest") for the same
# reason SpliceFormer pins a specific GitHub release tag: the exact
# weights GEPER downloads must never silently change underneath a
# running deployment. Zenodo records are immutable once published (a
# new version gets a new record id), so pinning the id is sufficient
# -- no separate ref/tag concept applies here.
DEFAULT_ZENODO_RECORD = "7995778"
_ARCHIVE_URL_TEMPLATE = (
    "https://zenodo.org/record/{record}/files/models.tar.gz?download=1"
)

# Three checkpoints ship inside the same archive (confirmed by listing
# the archive's own contents): `SpliceBERT.510nt`,
# `SpliceBERT-human.510nt` (human-only pretraining, fixed 510nt input),
# and `SpliceBERT.1024nt` (all 72 vertebrates, variable length up to
# 1024nt). `SpliceBERT.1024nt` is the flagship/most general checkpoint
# (matches the paper's own headline "72 vertebrates" pretraining set
# and supports the longest, variable-length input) -- the same
# "most general, officially flagship checkpoint" rationale
# EnformerPlugin/BorzoiPlugin/SpliceFormerPlugin's own default
# checkpoint choices already follow.
DEFAULT_CHECKPOINT = "SpliceBERT.1024nt"

# From the downloaded checkpoint's own config.json: max_position_embeddings
# is 1026 (1,024 content positions + [CLS] + [SEP]), vocab_size 10
# ([PAD],[UNK],[CLS],[SEP],[MASK],N,A,C,G,T -- single-nucleotide
# tokens, one per position, no k-mer/BPE merging). Not a free
# parameter here -- the pretrained weights below were trained with
# exactly this position-embedding table size.
MAX_POSITION_EMBEDDINGS = 1026
NUM_SPECIAL_TOKENS = 2  # [CLS] + [SEP], added by the tokenizer around the sequence
MAX_CONTENT_LENGTH = MAX_POSITION_EMBEDDINGS - NUM_SPECIAL_TOKENS  # 1,024


def archive_url(record: str = DEFAULT_ZENODO_RECORD) -> str:
    return _ARCHIVE_URL_TEMPLATE.format(record=record)


def checkpoint_dir_for(cache_dir: Path, checkpoint: str = DEFAULT_CHECKPOINT) -> Path:
    return cache_dir / checkpoint


def is_checkpoint_cached(cache_dir: Path, checkpoint: str = DEFAULT_CHECKPOINT) -> bool:
    """A cached checkpoint is considered complete once its config.json
    and weight file both exist -- config.json is written last by
    `download_and_extract_checkpoint` below, so its presence alone is
    already a reasonable "extraction finished" signal, and checking
    the weight file too guards against a hand-edited/partial cache
    directory."""
    directory = checkpoint_dir_for(cache_dir, checkpoint)
    return (directory / "config.json").is_file() and (directory / "pytorch_model.bin").is_file()


def download_and_extract_checkpoint(
    cache_dir: Path, checkpoint: str = DEFAULT_CHECKPOINT, record: str = DEFAULT_ZENODO_RECORD
) -> Path:
    """
    Downloads the official `models.tar.gz` archive (~208 MiB, all
    three checkpoints bundled together -- Zenodo does not support
    fetching a single subfolder of a tar.gz by itself) to a temporary
    location, extracts ONLY the `checkpoint` subfolder's files (not
    the other two checkpoints, to avoid keeping ~140 MiB of unused
    weights on disk), moves them into `cache_dir/{checkpoint}/`, and
    deletes the downloaded archive afterwards. Uses `requests`,
    already an unconditional GEPER dependency, exactly like
    `pipeline/models/spliceformer/loader.py::download_checkpoint`.

    Writes the archive to a `.part` sibling first and only extracts
    after a full, successful download, so an interrupted download
    never leaves a corrupt/partial checkpoint directory that
    `is_checkpoint_cached` would mistake for a good one.
    """
    import requests

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / "models.tar.gz"
    tmp_archive_path = archive_path.with_name(archive_path.name + ".part")

    url = archive_url(record)
    logger.info(f"Downloading SpliceBERT model archive from {url} ...")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_archive_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp_archive_path.replace(archive_path)
    logger.info(f"SpliceBERT archive saved to {archive_path}; extracting '{checkpoint}' ...")

    member_prefix = f"models/{checkpoint}/"
    extract_root = cache_dir / "_extract_tmp"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name.startswith(member_prefix) and m.isfile()]
            if not members:
                raise RuntimeError(
                    f"'{member_prefix}' not found in the downloaded SpliceBERT archive "
                    f"-- checkpoint name may be wrong, or upstream changed the archive layout."
                )
            tar.extractall(extract_root, members=members)

        destination = checkpoint_dir_for(cache_dir, checkpoint)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extract_root / "models" / checkpoint), str(destination))
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)
        archive_path.unlink(missing_ok=True)

    logger.info(f"SpliceBERT checkpoint '{checkpoint}' extracted to {destination}.")
    return destination


def build_model_and_tokenizer(checkpoint_dir: Path):
    """
    Loads the tokenizer and `BertForMaskedLM` weights from an already
    downloaded/extracted checkpoint directory via `transformers`'
    standard `AutoTokenizer`/`AutoModelForMaskedLM.from_pretrained`,
    exactly the way the official repo's own README documents loading
    it (`AutoTokenizer.from_pretrained(SPLICEBERT_PATH)` /
    `AutoModel.from_pretrained(SPLICEBERT_PATH)` -- `AutoModelForMaskedLM`
    here instead of the README's plain `AutoModel` because
    SpliceBERTPlugin needs the MLM head's per-token logits for
    zero-shot variant scoring, not just the encoder's hidden states;
    both share the same `BertForMaskedLM` checkpoint, `AutoModel`
    would simply discard the head). No custom model code -- the
    checkpoint's own `config.json` (`architectures:
    ["BertForMaskedLM"]`) is standard `transformers` BERT, imported
    here (not at this module's top level) so importing
    `splicebert_plugin.py` never itself requires `transformers` to be
    importable, mirroring `pipeline/models/spliceformer/loader.py
    ::build_model`'s deferred-import rationale.

    BUG FIX (found via a real Colab run that hung indefinitely right
    after "extracting SpliceBERT.1024nt", confirmed reproducible on
    plain CPU in this repo's own dev sandbox with no GPU involved):
    `AutoModelForMaskedLM.from_pretrained` never returns when
    TensorFlow is also importable in the same environment -- which it
    always is here, since `pipeline/models/mmsplice/` requires
    `tensorflow` unconditionally (requirements.txt). `transformers`
    auto-detects every installed backend (PyTorch/TensorFlow/Flax) and,
    for this specific checkpoint (`BertForMaskedLM`, a plain
    `pytorch_model.bin`, not `.safetensors`), that detection pathologically
    hangs rather than completing quickly. Verified fix: setting
    `USE_TF=0` before the `transformers` import skips TensorFlow-backend
    detection entirely -- confirmed to turn "never returns" into a
    fast, successful load (tokenizer ~0s, model ~1.3s once the one-time
    `transformers` import cost, ~20s, is paid). `os.environ.setdefault`
    (not a plain assignment) so an environment that has deliberately
    set `USE_TF` to something else for its own reasons is not silently
    overridden.
    """
    import os

    os.environ.setdefault("USE_TF", "0")

    from transformers import AutoModelForMaskedLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint_dir))
    model = AutoModelForMaskedLM.from_pretrained(str(checkpoint_dir))
    return model, tokenizer
