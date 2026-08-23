"""
RNA-FM wrapper.

Official ml4bio/RNA-FM (MIT license) embeds RNA sequences (transcribed
from the DNA context around a variant, U in place of T) to capture
RNA-level structural/functional signal -- e.g. for variants near splice
sites or in untranslated regions where RNA secondary structure matters.

Migrated from the MultiMolecule reimplementation (`multimolecule/rnafm`,
AGPL-3.0-or-later) to the official `rna-fm` PyPI package (MIT), which
avoids AGPL's copyleft/network-use obligations for a commercial product.
Confirmed equivalent: same architecture, same 640-dim embeddings,
verified numerically near-identical outputs on representative sequences
(see MIGRATION_RNA_FM.md) before this cutover.

RNA-FM's dependency (`rna-fm`, importable as `fm`) IS a normal PyPI
package (listed in requirements.txt) -- but a fresh environment where
`pip install -r requirements.txt` was skipped or only partially ran
(e.g. a Colab runtime restart) would still report RNA-FM as missing.
`is_available()`/`_load_impl()` below route through
`utils.auto_install.ensure_pip_package_available`, so RNA-FM
initializes itself the same way HyenaDNA does -- no manual setup step
required before running the pipeline. The pretrained weights are
fetched automatically on first use via `fm.pretrained`'s own
torch.hub-style download, cached under torch's default hub directory.

--------------------------------------------------------------------
Root cause of the "HTTP Error 403: Forbidden" failure (verified
against the real `rna-fm` 0.2.2 package source, `fm/pretrained.py`):

`fm.pretrained.rna_fm_t12()` does NOT fetch weights from PyPI, from
HuggingFace, or from any torch-hub-managed CDN. It hardcodes a single
academic download endpoint:

    https://proj.cse.cuhk.edu.hk/rnafm/api/download?filename=RNA-FM_pretrained.pth

fetched via `torch.hub.load_state_dict_from_url`, which issues a bare
`urllib` request with no browser-like `User-Agent`/`Referer`, and (as
of the package version this was verified against) with no timeout,
so a stalled connection can hang indefinitely rather than failing
fast. A single university lab server like this commonly 403s
automated `urllib`/`torch.hub` requests (bot/WAF filtering, rate
limiting, or an expired session token on the endpoint) even when the
same file is reachable from a normal browser. This is not a
deprecated API or a HuggingFace auth problem -- it's a fragile,
single-point, non-PyPI, non-CDN download host baked into the
upstream `rna-fm` package's own code.

The RNA-FM authors themselves have since published the *exact same*
weight files on an official, Apache-2.0-licensed Hugging Face mirror
(`cuhkaih/rnafm` -- same CUHK team as the GitHub repo; their own
README now points users there first). That HF mirror is the primary
weight source below; the original endpoint is kept only as a final,
last-resort fallback for backward compatibility, so:

  0. Try the official HF mirror (`cuhkaih/rnafm`) first, if not
     already cached -- a real, reliable, official weight source
     instead of the flaky lab server.
  1. Check for weights already cached locally (either from a prior
     successful download -- from either source -- or manually placed
     by the operator) and use them without ever touching the network.
  2. If neither of those worked, fall back to the upstream package's
     own endpoint, retrying only for errors that look transient
     (timeouts, connection resets) -- never for a definite HTTP error
     status like 403/404, where retrying is a waste of time.
  3. On any unrecoverable failure, skip RNA-FM gracefully: raise a
     short, sanitized `ModelLoadError` ("RNA-FM model unavailable")
     with zero raw network/HTTP text in the message the orchestrator
     and clinical report ever see. The full technical detail (the
     real exception, its type, and a traceback) is always logged via
     `self.logger.debug(..., exc_info=True)` for engineers, never
     surfaced to the report layer.
--------------------------------------------------------------------
"""

import contextlib
import pickle
import shutil
import socket
import time
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

import torch

from config import CONFIG
from models.base_model import BaseGenomicModel
from utils.auto_install import (
    PackageCheckStatus,
    check_pip_package_availability,
    ensure_pip_package_available,
    is_pip_package_installed,
)
from utils.exceptions import ModelLoadError

# Download filenames `fm.pretrained` itself hardcodes per variant (see
# `fm/pretrained.py::load_fm_model_and_alphabet_hub`) -- reproduced here
# only so we can *check for* an already-cached file before attempting
# any network access, not to duplicate the download logic itself.
# An unrecognized variant (e.g. a future `fm` release adds one) simply
# isn't pre-checked; it still works, just always goes through the
# normal (network-first) path below.
_RNA_FM_CHECKPOINT_FILENAMES = {
    "rna_fm_t12": "RNA-FM_pretrained.pth",
    "mrna_fm_t12": "file_mRNA-FM_pretrained.pth",
}

# Only these look transient (worth one or two retries with backoff).
# A definite HTTP status (403 Forbidden, 404 Not Found, etc) is not
# transient -- retrying it just wastes time before the same graceful
# skip -- so `urllib.error.HTTPError` (a subclass of `URLError`) is
# deliberately excluded from the retry path, not just left to fall
# through to it.
_TRANSIENT_NETWORK_ERRORS = (TimeoutError, ConnectionError)

_MAX_DOWNLOAD_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# Bounds each download attempt so a connection that stalls (accepted
# but never responds) fails fast into the retry/skip logic above
# instead of hanging forever -- see the timeout comment in
# `_load_pretrained_with_fallback` for the full rationale.
_DOWNLOAD_TIMEOUT_SECONDS = 30.0

# Official, Apache-2.0-licensed Hugging Face mirror of the exact same
# RNA-FM/mRNA-FM weight files, published by the RNA-FM authors' own
# CUHK team (huggingface.co/cuhkaih/rnafm) -- their GitHub README now
# points users here directly. Tried first, before the upstream
# package's own unreliable proj.cse.cuhk.edu.hk endpoint.
_HF_MIRROR_REPO_ID = "cuhkaih/rnafm"
_HF_MIRROR_FILENAMES = {
    "rna_fm_t12": "RNA-FM_pretrained.pth",
    "mrna_fm_t12": "mRNA-FM_pretrained.pth",
}
_HF_MIRROR_TIMEOUT_SECONDS = 10.0


def is_rna_fm_installed() -> bool:
    """Cheap, import-free check for whether the official `rna-fm` package
    (imported as `fm`) is installed."""
    return is_pip_package_installed("fm")


def ensure_rna_fm_available() -> bool:
    """Auto-installs the official `rna-fm` PyPI package exactly once per
    process if missing. Import name is `fm`, distinct from the pip
    package name `rna-fm` -- see utils.auto_install.ensure_pip_package_available.

    Bool surface, for callers indifferent to WHY it is unavailable. If
    you are about to write a reason string on the False branch, call
    `check_rna_fm_availability()` below instead."""
    return ensure_pip_package_available("rna-fm", import_name="fm")


def check_rna_fm_availability() -> PackageCheckStatus:
    """Tri-state sibling of `ensure_rna_fm_available()`, for callers that
    must distinguish "checked and absent" from "never checked".

    Exists so the `rna-fm`/`fm` pip-name-vs-import-name mapping stays
    encapsulated here -- the reason the bool wrapper existed at all --
    rather than leaking to every call site that needs the tri-state."""
    return check_pip_package_availability("rna-fm", import_name="fm")


def _torch_hub_checkpoint_path(filename: str) -> Path:
    """Where `torch.hub.load_state_dict_from_url` caches/looks for a
    given checkpoint filename -- same directory torch.hub itself uses,
    so a weights file already downloaded by a prior run (or placed
    there manually, e.g. copied from a machine with working network
    access to the CUHK host) is found and reused without ever
    re-attempting a network fetch."""
    return Path(torch.hub.get_dir()) / "checkpoints" / filename


def _cached_weights_path(model_variant: str) -> Optional[Path]:
    """Return the path to already-cached weights for `model_variant`,
    or None if not cached (or the variant isn't one we know the
    filename for -- see `_RNA_FM_CHECKPOINT_FILENAMES`)."""
    filename = _RNA_FM_CHECKPOINT_FILENAMES.get(model_variant)
    if filename is None:
        return None
    path = _torch_hub_checkpoint_path(filename)
    return path if path.is_file() else None


def _fetch_from_official_hf_mirror(model_variant: str, logger) -> Optional[Path]:
    """
    Downloads the real, unmodified RNA-FM/mRNA-FM weight file from the
    RNA-FM authors' own official Hugging Face mirror
    (huggingface.co/cuhkaih/rnafm, Apache-2.0) directly into torch
    hub's checkpoint cache dir, under the exact filename
    `fm.pretrained` itself expects there -- so it's indistinguishable
    from a manually-placed file to the cache-check step above (and to
    every future run, without touching the network again).

    This exists because the official `rna-fm` PyPI package still
    hardcodes the old, unreliable proj.cse.cuhk.edu.hk endpoint (see
    module docstring); Hugging Face's CDN is a far more reliable
    source for the exact same weights.

    Returns the local path on success, or None if this attempt itself
    failed for any reason (huggingface_hub unavailable, network
    error, unrecognized variant, ...) -- callers must treat None as
    "fall through to the next strategy", never as fatal.
    """
    hf_filename = _HF_MIRROR_FILENAMES.get(model_variant)
    local_filename = _RNA_FM_CHECKPOINT_FILENAMES.get(model_variant)
    if hf_filename is None or local_filename is None:
        return None

    if not ensure_pip_package_available("huggingface_hub", import_name="huggingface_hub"):
        return None

    import huggingface_hub

    dest_path = _torch_hub_checkpoint_path(local_filename)
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_HF_MIRROR_TIMEOUT_SECONDS)
    try:
        downloaded_path = huggingface_hub.hf_hub_download(
            repo_id=_HF_MIRROR_REPO_ID,
            filename=hf_filename,
            etag_timeout=_HF_MIRROR_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - this is a best-effort primary path
        logger.debug(
            f"Official HF mirror ('{_HF_MIRROR_REPO_ID}/{hf_filename}') "
            f"fetch failed ({exc.__class__.__name__}): {exc}; falling "
            "back to the upstream rna-fm endpoint.",
            exc_info=True,
        )
        return None
    finally:
        socket.setdefaulttimeout(previous_timeout)

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(downloaded_path, dest_path)
    except OSError as exc:
        logger.debug(
            f"Copying HF-mirrored RNA-FM checkpoint to '{dest_path}' "
            f"failed ({exc.__class__.__name__}): {exc}; falling back "
            "to the upstream rna-fm endpoint.",
            exc_info=True,
        )
        return None

    return dest_path


def _is_permanent_download_error(exc: Exception) -> bool:
    """True for errors where retrying is pointless (a definite HTTP
    status code, e.g. 403/404) -- as opposed to a transient network
    hiccup that a short retry-with-backoff might genuinely recover
    from."""
    return isinstance(exc, urllib.error.HTTPError)


def _is_unpickling_error(exc: Exception) -> bool:
    """True for the failure PyTorch >=2.6 raises against this exact
    checkpoint now that `torch.load`'s `weights_only` default flipped
    from `False` to `True` -- the file itself isn't corrupt, it's just
    a plain, trusted first-party checkpoint (same trust level as
    HyenaDNA's, which already loads with `weights_only=False`
    explicitly -- see hyenadna.py) that predates PyTorch's newer,
    stricter unpickling allowlist."""
    if isinstance(exc, pickle.UnpicklingError):
        return True
    return "weights_only" in str(exc)


@contextlib.contextmanager
def _force_weights_only_false():
    """
    `fm.pretrained.<variant>(model_location=...)` calls `torch.load(...)`
    internally with no `weights_only` argument, so there's no way to
    pass it through the official package's API. This temporarily
    patches `torch.load`'s default back to `weights_only=False` for the
    duration of the call instead, so a checkpoint that only fails
    because of PyTorch 2.6's new default can be loaded from the exact
    same local file it already has -- instead of discarding a perfectly
    good file and wastefully re-downloading (or re-hitting the flaky
    upstream endpoint) for a problem re-downloading can't fix anyway.
    """
    original_load = torch.load

    def _patched_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return original_load(*args, **kwargs)

    torch.load = _patched_load
    try:
        yield
    finally:
        torch.load = original_load


def _load_local_checkpoint(loader, path: str, logger):
    """
    Loads an already-on-disk RNA-FM checkpoint via the official
    `fm.pretrained` loader, retrying once with `weights_only` forced to
    `False` (see `_force_weights_only_false`) if the first attempt
    fails with an unpickling error. Any other failure (a genuinely
    truncated/corrupt file, an unexpected format, ...) is re-raised
    immediately for the caller's existing fallback chain to handle.
    """
    try:
        return loader(model_location=path)
    except Exception as exc:  # noqa: BLE001 - re-raised below unless recognized
        if not _is_unpickling_error(exc):
            raise
        logger.info(
            f"RNA-FM checkpoint at '{path}' failed to load under "
            f"PyTorch's newer weights_only=True default "
            f"({exc.__class__.__name__}); retrying the same local file "
            "with weights_only=False instead of re-downloading."
        )
        with _force_weights_only_false():
            return loader(model_location=path)


class RNAFMModel(BaseGenomicModel):
    """Embeds RNA sequences using the official RNA-FM (ml4bio/RNA-FM)."""

    def cache_key(self) -> str:
        return "rna_fm"

    @classmethod
    def is_available(cls) -> bool:
        # Auto-installs on first call, same rationale as
        # HyenaDNAModel.is_available(): the orchestrator's up-front
        # availability probe should reflect the environment after
        # setup, not before it.
        return ensure_rna_fm_available()

    @classmethod
    def unavailability_reason(cls) -> str:
        """Overrides `BaseGenomicModel`'s generic "not installed", which
        cannot tell a package that was checked and found missing from one
        whose availability was never checked at all."""
        status = check_rna_fm_availability()
        if status is PackageCheckStatus.NOT_CHECKED:
            return "availability not checked (auto-install disabled under pytest)"
        if status is PackageCheckStatus.ABSENT:
            return "not installed ('pip install rna-fm' did not succeed)"
        return "not installed"

    def _load_impl(self):
        rna_fm_status = check_rna_fm_availability()
        if rna_fm_status is PackageCheckStatus.NOT_CHECKED:
            raise ModelLoadError(
                "RNA-FM requires the official 'rna-fm' package, whose "
                "availability was not checked in this environment "
                "(auto-install is disabled under pytest) -- it is not "
                "confirmed missing, it was never looked for. Install it "
                "with `pip install rna-fm`, or run outside the test "
                "harness, to find out."
            )
        if rna_fm_status is PackageCheckStatus.ABSENT:
            raise ModelLoadError(
                "RNA-FM requires the official 'rna-fm' package, and automatic "
                "installation ('pip install rna-fm') did not "
                "succeed in this environment -- check network access to "
                "pypi.org, or install it yourself with "
                "`pip install rna-fm`."
            )

        import fm

        model_variant = CONFIG.models.RNA_FM  # e.g. "rna_fm_t12"
        loader = getattr(fm.pretrained, model_variant, None)
        if loader is None:
            raise ModelLoadError(
                f"Unknown RNA-FM pretrained variant '{model_variant}' -- "
                "expected an attribute of fm.pretrained (e.g. 'rna_fm_t12')."
            )

        model, alphabet = self._load_pretrained_with_fallback(loader, model_variant)
        self.model = model
        self.alphabet = alphabet
        self.batch_converter = alphabet.get_batch_converter()
        self.model.to(self.device)
        self.model.eval()

    def _load_pretrained_with_fallback(self, loader, model_variant: str):
        """
        Loads RNA-FM weights, preferring an already-cached local copy
        and never letting a raw network/HTTP error escape to the
        caller. See the module docstring for the full root-cause
        writeup of the "HTTP Error 403" failure this replaces.
        """
        # 1. Cache check: reuse an already-downloaded (or manually
        # placed) checkpoint without touching the network at all.
        # `fm.pretrained.rna_fm_t12(model_location=...)` is the
        # official package's own local-file loading path (used
        # verbatim, not reimplemented), so this is guaranteed
        # compatible with whatever format the loader itself produces.
        cached_path = _cached_weights_path(model_variant)
        if cached_path is not None:
            self.logger.info(
                f"Using cached RNA-FM weights for '{model_variant}' at '{cached_path}' -- skipping network download."
            )
            try:
                return _load_local_checkpoint(loader, str(cached_path), self.logger)
            except Exception as exc:  # noqa: BLE001
                # A cached file that fails to load (corrupted partial
                # download, wrong format) shouldn't be trusted silently
                # -- fall through to a fresh network attempt instead,
                # which is more likely to succeed than reusing bad
                # local bytes. (An UnpicklingError caused only by
                # PyTorch 2.6's weights_only default was already
                # retried in-place by _load_local_checkpoint above, so
                # reaching this branch means it genuinely didn't help.)
                self.logger.warning(
                    f"Cached RNA-FM checkpoint at '{cached_path}' failed "
                    f"to load ({exc.__class__.__name__}); ignoring cache "
                    "and attempting a fresh download.",
                    exc_info=True,
                )

        # 1b. Not cached -- try the official HF mirror before ever
        # touching the upstream package's own unreliable endpoint.
        # Reuses the exact same trusted `loader(model_location=...)`
        # local-file path as the cache-hit branch above, just pointed
        # at a freshly-downloaded (rather than pre-existing) file.
        if cached_path is None:
            mirror_path = _fetch_from_official_hf_mirror(model_variant, self.logger)
            if mirror_path is not None:
                try:
                    self.logger.info(
                        f"Downloaded RNA-FM weights for '{model_variant}' "
                        f"from the official HF mirror "
                        f"'{_HF_MIRROR_REPO_ID}' -- skipping the "
                        "unreliable upstream endpoint."
                    )
                    return _load_local_checkpoint(loader, str(mirror_path), self.logger)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        f"HF-mirrored RNA-FM checkpoint at '{mirror_path}' "
                        f"failed to load ({exc.__class__.__name__}); "
                        "falling back to the upstream endpoint.",
                        exc_info=True,
                    )

        # 2. Network download, with a bounded retry for genuinely
        # transient errors only (see _is_permanent_download_error).
        #
        # `loader()` -> `torch.hub.load_state_dict_from_url` issues a
        # bare `urllib` request with NO timeout by default (Python's
        # socket default is "block forever"). Most of the time the
        # CUHK host fast-fails with a 403 (see module docstring) and
        # this never matters -- but if the host instead accepts the
        # connection and then stalls, `loader()` hangs indefinitely
        # with nothing for the retry loop below to ever catch, since
        # no exception is ever raised. Bounding it with
        # `socket.setdefaulttimeout` turns a stall into a
        # `TimeoutError`, which the retry loop below already treats as
        # transient and retryable -- so this only supplies the missing
        # timeout, it doesn't change the retry/skip behavior itself.
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_DOWNLOAD_ATTEMPTS + 1):
            previous_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(_DOWNLOAD_TIMEOUT_SECONDS)
            try:
                return loader()
            except Exception as exc:  # noqa: BLE001 - translate + sanitize below
                if _is_unpickling_error(exc):
                    # The download itself succeeded (torch.hub already
                    # cached the file); only the unpickling step failed,
                    # under PyTorch 2.6's new weights_only=True default.
                    # Retry the load in place instead of letting this
                    # be treated as a corrupt/permanent failure below,
                    # which would otherwise skip RNA-FM for the whole
                    # run over a problem that was never about the file.
                    try:
                        with _force_weights_only_false():
                            return loader()
                    except Exception as retry_exc:  # noqa: BLE001
                        exc = retry_exc
                last_exc = exc
                permanent = _is_permanent_download_error(exc)
                self.logger.debug(
                    f"RNA-FM weight download attempt {attempt}/"
                    f"{_MAX_DOWNLOAD_ATTEMPTS} for '{model_variant}' failed "
                    f"({exc.__class__.__name__}, "
                    f"{'permanent' if permanent else 'possibly transient'}): "
                    f"{exc}",
                    exc_info=True,
                )
                if permanent or not isinstance(exc, _TRANSIENT_NETWORK_ERRORS + (urllib.error.URLError,)):
                    # Definite HTTP error (403/404/...), or something
                    # that isn't a recognized network error at all
                    # (e.g. a corrupt-checkpoint RuntimeError) --
                    # retrying won't help either way.
                    break
                if attempt < _MAX_DOWNLOAD_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
            finally:
                socket.setdefaulttimeout(previous_timeout)

        # 3. Graceful, sanitized skip. Intentionally short and free of
        # any raw network/HTTP text (status codes, hostnames, urllib
        # exception text) -- this exact string is what can end up in
        # a per-variant report's "Skipped: ..." line via the
        # orchestrator's existing `str(exc)` handling, so it must
        # never expose implementation/network detail on its own.
        raise ModelLoadError("RNA-FM model unavailable") from last_exc

    def reported_max_length(self):
        return self._resolve_safe_max_length(CONFIG.models.RNA_FM_MAX_SAFE_TOKENS)

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        # RNA-FM expects RNA alphabet (A, C, G, U, N). Guard against
        # callers accidentally passing a DNA sequence.
        rna_sequence = sequence.upper().replace("T", "U")

        # Defensive cap, same rationale as HyenaDNA/ESM-2: the RNA
        # context is transcribed 1:1 from the DNA window, which can be
        # multi-kb for structural-variant-scale windows -- never let
        # that overflow RNA-FM's trained context and risk a CUDA error.
        max_length = kwargs.get("max_length") or self._resolve_safe_max_length(CONFIG.models.RNA_FM_MAX_SAFE_TOKENS)
        # The official `fm` API has no HF-tokenizer-style `truncation=`
        # kwarg -- truncate the raw sequence ourselves, leaving room for
        # the BOS/EOS special tokens the alphabet's batch_converter adds,
        # to preserve the same "never exceed max_length tokens" contract
        # the MultiMolecule tokenizer enforced.
        num_special_tokens = int(self.alphabet.prepend_bos) + int(self.alphabet.append_eos)
        max_chars = max(1, max_length - num_special_tokens)
        truncated = len(rna_sequence) > max_chars
        if truncated:
            rna_sequence = rna_sequence[:max_chars]
            self.logger.warning(
                f"RNA sequence length {len(sequence)} produced >= "
                f"{max_length} tokens for RNA-FM; truncated to the "
                "model's safe limit."
            )

        _, _, tokens = self.batch_converter([("seq0", rna_sequence)])
        tokens = tokens.to(self.device)

        with torch.no_grad():
            results = self.model(tokens, repr_layers=[self.model.num_layers])
        hidden_states = results["representations"][self.model.num_layers]

        # Single-sequence batch (no padding introduced): every token is
        # real, so the attention mask is all ones -- matches the prior
        # HF-tokenizer-derived mask's semantics (mean-pool over all
        # non-pad tokens, including BOS/EOS special tokens).
        attention_mask = torch.ones(hidden_states.shape[:2], device=hidden_states.device)

        pooled = self.mean_pool(hidden_states, attention_mask)

        return {
            "embedding_mean": pooled.squeeze(0).cpu().tolist(),
            "embedding_dim": hidden_states.shape[-1],
            "num_tokens": int(hidden_states.shape[1]),
        }
