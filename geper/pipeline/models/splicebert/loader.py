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

    BUG FIX #1, ROOT CAUSE (originally addressed with `USE_TF=0`,
    reopened by a real 2+-hour hang reported against that fix -- see
    `_force_transformers_to_prefer_torch_over_tf`'s docstring for the
    full story of why setting the `USE_TF` environment variable here
    was not sufficient on its own): `transformers` caches whether
    TensorFlow is "available" exactly once, the first time
    `transformers.utils.import_utils` itself is imported by ANYTHING
    in the process -- not just by this function. `pipeline/models/esm2.py`
    (`from transformers import AutoTokenizer, EsmModel`) always runs
    first, during the orchestrator's startup model validation, well
    before this function ever executes -- so by the time `USE_TF=0` is
    set here, `transformers` is already imported and that decision is
    already permanently cached (as "TensorFlow available", since
    `pipeline/models/mmsplice/` requires `tensorflow` unconditionally).
    The env var is kept below anyway (harmless, and correct for any
    caller where this genuinely is the first `transformers` import),
    but the real fix is forcing the already-cached flag directly --
    see that helper.

    BUG FIX #2 (this function): the load is now wrapped in a bounded
    timeout (`CONFIG.splicing.SPLICEBERT_LOAD_TIMEOUT_SECS`, default
    180s). Fix #1 is confirmed (by direct reproduction) to turn the
    reported multi-hour hang into a sub-second load in this repo's own
    dev sandbox, but a silent, indefinite hang is exactly the kind of
    failure mode that should never be trusted to be fully eliminated
    by a single upstream-library-internals fix across every
    `transformers`/`tensorflow` version combination a deployment might
    have -- if this ever regresses again (a future `transformers`
    release renaming/removing the internal flag Fix #1 patches, for
    example), the pipeline must fail loudly with a clear, actionable
    error and let `SpliceBERTPlugin` demote to unavailable (the
    existing graceful-degradation path every other model already
    uses), never hang the whole run silently again.
    """
    import os

    os.environ.setdefault("USE_TF", "0")

    from transformers import AutoModelForMaskedLM, AutoTokenizer

    _force_transformers_to_prefer_torch_over_tf()

    return _load_with_timeout(checkpoint_dir, AutoModelForMaskedLM, AutoTokenizer)


def _force_transformers_to_prefer_torch_over_tf() -> None:
    """
    Directly overrides `transformers.utils.import_utils._tf_available`
    -- the already-imported, already-cached module attribute
    `is_tf_available()` (and every downstream `AutoModelForMaskedLM`
    code path that branches on it) actually reads -- rather than
    relying on the `USE_TF` environment variable, which only has any
    effect the very first time `transformers.utils.import_utils` is
    imported in this process. Confirmed by direct reproduction: with
    `transformers` already imported (simulating `models/esm2.py`
    loading first, as it always does in a real GEPER run), setting
    `USE_TF=0` immediately before `from transformers import ...` had
    no effect (this checkpoint's load still triggered the slow/
    pathological TensorFlow-backend-detection path); overriding this
    attribute directly, immediately before loading, did.

    This reaches into a "private" (leading-underscore) attribute of a
    third-party library, which is unusual and normally worth avoiding
    -- justified here because (a) the public, supported mechanism
    (the `USE_TF` env var) is provably too late by the time this
    function can run in a real pipeline, and (b) the override is
    wrapped in `try/except` below, so if a future `transformers`
    release renames or removes this attribute, the worst outcome is
    silently falling back to Fix #1's env-var-only behavior (which
    still works whenever this does happen to be the first
    `transformers` import) plus Fix #2's timeout below -- never a hard
    crash from this defensive patch itself.
    """
    try:
        from transformers.utils import import_utils as _transformers_import_utils

        _transformers_import_utils._tf_available = False
    except Exception as exc:  # noqa: BLE001 - best-effort defensive patch, see docstring
        logger.debug(
            f"Could not force transformers to prefer the PyTorch backend "
            f"(internal attribute may have changed in this transformers "
            f"version): {exc}. Falling back to the USE_TF env var and the "
            f"load timeout alone.",
            exc_info=True,
        )


def _load_with_timeout(checkpoint_dir: Path, model_cls, tokenizer_cls):
    """
    Runs the actual `from_pretrained` calls in a worker thread and
    waits up to `CONFIG.splicing.SPLICEBERT_LOAD_TIMEOUT_SECS` for them
    to finish, raising `TimeoutError` (caught by
    `SpliceBERTPlugin._load_impl`, which demotes this model to
    unavailable for the rest of the run -- the same graceful-
    degradation path every other model already uses) instead of
    hanging the whole pipeline silently if they don't.

    Known, disclosed limitation: CPython cannot forcibly kill a thread
    that is itself stuck (e.g. in a C extension or blocked on I/O with
    no timeout of its own), so a worker that truly never returns keeps
    running in the background, using memory/CPU, until it either
    finishes on its own or the process exits. What this DOES guarantee
    is that the *caller* stops waiting and reports a clear, actionable
    error at a bounded time -- turning a silent multi-hour hang into an
    immediate, diagnosable failure, which is the actual goal.
    """
    import concurrent.futures

    from config import CONFIG

    def _load():
        tokenizer = tokenizer_cls.from_pretrained(str(checkpoint_dir))
        model = model_cls.from_pretrained(str(checkpoint_dir))
        return model, tokenizer

    timeout = CONFIG.splicing.SPLICEBERT_LOAD_TIMEOUT_SECS
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="splicebert-load")
    future = executor.submit(_load)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(
            f"Loading SpliceBERT checkpoint from '{checkpoint_dir}' did not complete within "
            f"{timeout:.0f}s (GEPER_SPLICEBERT_LOAD_TIMEOUT_SECS) -- treating this as a load "
            f"failure rather than waiting indefinitely. This previously manifested as a "
            f"multi-hour silent hang caused by transformers' TensorFlow-backend detection; "
            f"see build_model_and_tokenizer's docstring."
        ) from exc
    finally:
        # Don't block process shutdown on a worker that may never
        # return (see docstring) -- `wait=False` lets the interpreter
        # exit without joining it.
        executor.shutdown(wait=False)
