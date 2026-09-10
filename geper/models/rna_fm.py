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

import argparse
import os
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


def _is_weights_only_rejection(exc: Exception) -> bool:
    """
    True when `exc` is torch's own `weights_only=True` rejection of a
    pickled class it does not recognise -- e.g. an already-cached
    fairseq checkpoint whose format has drifted one field ahead of
    `_allow_argparse_namespace_in_checkpoints`'s current allowlist --
    as opposed to a genuinely corrupted or unreadable file.

    torch.load raises exactly `pickle.UnpicklingError` for this specific
    case (see torch/serialization.py:
    `raise pickle.UnpicklingError(_get_wo_message(str(e))) from None`),
    and the message names the rejected global. Both are checked --
    UnpicklingError is torch's error class for this, but the message
    check keeps an unrelated pickle-stream corruption that happens to
    raise the same exception TYPE from being misclassified as an
    allowlist gap.

    Measured, not assumed: a truncated/corrupted real checkpoint raises
    `RuntimeError: PytorchStreamReader failed reading zip archive:
    failed finding central directory` instead -- a torch checkpoint is
    a zip archive whose central directory sits near the end of the
    file, so ordinary corruption breaks archive-opening itself, before
    unpickling-mode differences ever matter. That is the case this
    function must say NO to.
    """
    return isinstance(exc, pickle.UnpicklingError) and (
        "Unsupported global" in str(exc) or "Weights only load failed" in str(exc)
    )


def _quarantine_unreadable_checkpoint(path: Path, logger) -> None:
    """
    Renames a checkpoint that failed to load aside (never deletes) so a
    subsequent retry cannot find it still present at the exact path the
    cache-check (or torch.hub's own cache lookup) expects, and silently
    re-read the SAME bad bytes through a different, less strict
    unpickling path -- the risk this function exists to remove is
    narrowed, not fully closed, by the caller's own exception
    classification alone: "corrupted, fall through to a fresh download"
    only actually gets FRESH bytes if the corrupted file is no longer
    sitting at the path the next attempt will look for.

    Best-effort: a rename failure (e.g. read-only filesystem) is logged
    and swallowed -- this is a defence-in-depth improvement to an
    already-graceful fallback path, not a new way for loading to fail
    harder than it did before this existed.
    """
    quarantined = path.with_name(f"{path.name}.corrupted-{int(time.time())}-{os.getpid()}")
    try:
        path.rename(quarantined)
        logger.warning(f"Moved unreadable RNA-FM checkpoint aside to '{quarantined}' before retrying.")
    except OSError as exc:
        logger.warning(
            f"Could not move unreadable RNA-FM checkpoint '{path}' aside "
            f"({exc.__class__.__name__}: {exc}) -- a retry may find the same bad file again."
        )


def _allow_argparse_namespace_in_checkpoints() -> None:
    """
    Registers `argparse.Namespace` on PyTorch's strict-unpickling
    allowlist so the RNA-FM checkpoint loads with `weights_only` left
    at PyTorch's own default. Idempotent -- safe to call on every load.

    WHAT IN THE FILE TRIPS THE NEWER DEFAULT. PyTorch 2.6 flipped
    `torch.load`'s `weights_only` default from `False` to `True`. The
    RNA-FM checkpoint is a fairseq-style *training* checkpoint, not a
    bare state dict: alongside the 206-tensor `model` state dict it
    carries `args`, `cfg`, optimizer history and task state. The one
    and only thing in it the strict unpickler rejects is
    `argparse.Namespace`, at exactly `.args` and `.cfg.model`:

        WeightsUnpickler error: Unsupported global: GLOBAL
        argparse.Namespace was not an allowed global by default.

    Nothing is wrong with the file. It is a trusted first-party
    checkpoint that simply predates PyTorch's stricter unpickling
    allowlist. HyenaDNA's checkpoint has the same problem and is
    handled the same way (`_allow_lightning_checkpoint_globals` in
    hyenadna.py) -- but with a DIFFERENT set of classes, established
    there by its own probe: a Lightning checkpoint carries omegaconf
    config containers, not an `argparse.Namespace`. The set has to be
    measured per checkpoint, never carried across.

    WHY THE OBVIOUS FIX -- RE-SAVING THE CHECKPOINT WITHOUT THE
    `Namespace` -- DOES NOT WORK. `args`/`cfg` are load-bearing for
    `fm.pretrained` itself. Two re-save forms were tested through the
    official loader with `torch.load` unpatched: stripping the file to
    `{"model": ...}`, and keeping the full structure with every
    `Namespace` converted to a plain dict. Both got past
    `weights_only=True` and then died further along with
    `FileNotFoundError: <variant>-contact-regression.pt` -- the library
    reads that metadata, and without it takes a branch demanding a
    companion file that does not exist beside the checkpoint. So a
    re-save satisfies the unpickler and breaks the loader. This is NOT
    a claim that no correct re-save is possible, only that neither
    obvious form works, and why. If that route is ever revisited the
    weights themselves are portable: a SHA-256 over all 206 tensors was
    identical before and after re-saving.

    WHY AN ALLOWLIST RATHER THAN `weights_only=False`. Forcing
    `weights_only=False` permits arbitrary code execution during
    unpickling, for every object in the file. This keeps strict
    unpickling switched on for everything else and permits exactly one
    inert data-holder class.

    CAVEAT, ACCEPTED KNOWINGLY: `add_safe_globals` is process-global.
    Once called, any `torch.load` in this process will also accept an
    `argparse.Namespace`. That is far narrower than the blanket
    `torch.load` patch this replaces, but it is a real property of the
    change, named here so it is chosen deliberately rather than
    inherited silently.
    """
    torch.serialization.add_safe_globals([argparse.Namespace])


def _load_local_checkpoint(loader, path: str):
    """
    Loads an already-on-disk RNA-FM checkpoint via the official
    `fm.pretrained` loader. `weights_only` is left at PyTorch's own
    default; `argparse.Namespace` is allowlisted first (see
    `_allow_argparse_namespace_in_checkpoints`) so the checkpoint's
    training metadata does not trip the strict unpickler. Any failure
    is re-raised for the caller's existing fallback chain to handle.
    """
    _allow_argparse_namespace_in_checkpoints()
    return loader(model_location=path)


def _load_hub_download_with_forced_weights_only(loader):
    """
    Calls `loader()` -- `fm.pretrained.rna_fm_t12` with no
    `model_location`, i.e. Meta's own `load_hub_workaround` -> REAL
    `torch.hub.load_state_dict_from_url` -- with PyTorch's strict
    `weights_only` unpickling FORCED to `True` for the duration of this
    one call, closing the ATTACK half of the residual named at
    `_load_pretrained_with_fallback`'s step 2 (human-ruled,
    2026-09-10): a GENUINE FRESH download carrying a pickled class
    outside `_allow_argparse_namespace_in_checkpoints`'s allowlist must
    be refused, not silently accepted through
    `load_state_dict_from_url`'s own hardcoded `weights_only=False`
    default.

    HOW, WITHOUT PRE-FETCHING OR PRE-PLACING ANYTHING. `load_hub_workaround`
    (fm/pretrained.py, confirmed by reading its installed source) calls
    `torch.hub.load_state_dict_from_url(...)` by attribute lookup at
    call time, not a name bound once at import time -- so replacing the
    module attribute for the duration of this call reaches it.
    `load_state_dict_from_url` itself forwards `weights_only` straight
    through to its own internal `torch.load(..., weights_only=weights_only)`
    (confirmed by reading torch's installed source) -- there is no
    separate strict/local attempt to build here, no cache-filename
    convention to duplicate, and torch.hub still resolves its own URL,
    cache path, and network request exactly as before. This is the
    reason the ruling did not ask for pre-placement: pre-placement was
    argued against (it couples this file to torch.hub's cache-filename
    convention, which is load-bearing for offline loading) and this
    sidesteps that coupling entirely by never touching the file's
    location or provenance -- only the flag PyTorch itself unpickles
    with.

    Because `_allow_argparse_namespace_in_checkpoints` is always called
    before this runs (see the retry loop below), an ordinary legitimate
    checkpoint -- whose only non-default-safe class is the allowlisted
    `argparse.Namespace` -- still succeeds under this strict call. Only
    a class OUTSIDE that allowlist is refused, loudly, by name, so the
    reader knows what to update.
    """
    real_load_state_dict_from_url = torch.hub.load_state_dict_from_url

    def strict_load_state_dict_from_url(*args, **kwargs):
        kwargs["weights_only"] = True
        return real_load_state_dict_from_url(*args, **kwargs)

    torch.hub.load_state_dict_from_url = strict_load_state_dict_from_url
    try:
        return loader()
    except Exception as exc:  # noqa: BLE001
        if _is_weights_only_rejection(exc):
            raise ModelLoadError(
                "Freshly-downloaded RNA-FM checkpoint contains a pickled "
                "class outside today's allowlist "
                "(_allow_argparse_namespace_in_checkpoints) -- refusing "
                "to load it with an unrestricted (weights_only=False) "
                f"fallback. Update the allowlist for this checkpoint "
                f"format. Original error: {exc}"
            ) from exc
        raise
    finally:
        torch.hub.load_state_dict_from_url = real_load_state_dict_from_url


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
                return _load_local_checkpoint(loader, str(cached_path))
            except Exception as exc:  # noqa: BLE001
                if _is_weights_only_rejection(exc):
                    # HIGH-priority security card: a cached file that
                    # fails ONLY because it carries a pickled class
                    # outside today's allowlist is NOT corruption -- it
                    # is ordinary upstream checkpoint-format drift on a
                    # file we already hold. The old code below ("shrug,
                    # try a wider unpickling path") silently re-read the
                    # SAME bytes through torch.hub's own
                    # weights_only=False default, reintroducing exactly
                    # the risk the allowlist exists to remove, on a file
                    # that was never corrupted at all. This is the
                    # ACCIDENT case (drift on a file we already hold),
                    # and stopping here -- never reaching step 2's wide
                    # path for THIS file -- closes it.
                    raise ModelLoadError(
                        f"Cached RNA-FM checkpoint at '{cached_path}' contains a "
                        "pickled class outside today's allowlist "
                        "(_allow_argparse_namespace_in_checkpoints) -- this is not "
                        "corruption, and must not be recovered by falling back to "
                        "an unrestricted (weights_only=False) load. Update the "
                        f"allowlist for this checkpoint format. Original error: {exc}"
                    ) from exc
                # A genuinely corrupted cached file (partial download,
                # filesystem error) shouldn't be trusted silently --
                # move it aside so the retry below cannot silently
                # re-read the SAME bad bytes through a wider unpickling
                # path (torch.hub's own cache lookup would otherwise
                # find it still sitting at this exact path and treat it
                # as already-downloaded), then fall through to a fresh
                # network attempt.
                self.logger.warning(
                    f"Cached RNA-FM checkpoint at '{cached_path}' failed "
                    f"to load ({exc.__class__.__name__}); ignoring cache "
                    "and attempting a fresh download.",
                    exc_info=True,
                )
                _quarantine_unreadable_checkpoint(cached_path, self.logger)

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
                    return _load_local_checkpoint(loader, str(mirror_path))
                except Exception as exc:  # noqa: BLE001
                    if _is_weights_only_rejection(exc):
                        # Same treatment as the cache-hit branch above --
                        # see that comment for the full reasoning.
                        raise ModelLoadError(
                            f"HF-mirrored RNA-FM checkpoint at '{mirror_path}' "
                            "contains a pickled class outside today's "
                            "allowlist (_allow_argparse_namespace_in_checkpoints) "
                            "-- this is not corruption, and must not be recovered "
                            "by falling back to an unrestricted (weights_only=False) "
                            f"load. Update the allowlist. Original error: {exc}"
                        ) from exc
                    self.logger.warning(
                        f"HF-mirrored RNA-FM checkpoint at '{mirror_path}' "
                        f"failed to load ({exc.__class__.__name__}); "
                        "falling back to the upstream endpoint.",
                        exc_info=True,
                    )
                    _quarantine_unreadable_checkpoint(mirror_path, self.logger)

        # 2. Network download, with a bounded retry for genuinely
        # transient errors only (see _is_permanent_download_error).
        #
        # *** ATTACK-HALF RESIDUAL, CLOSED (HIGH-priority security card;
        # human-ruled 2026-09-10): `loader()` below is
        # `fm.pretrained.rna_fm_t12` with no `model_location`, which is
        # Meta's own `load_hub_workaround` -> REAL
        # `torch.hub.load_state_dict_from_url` -- and THAT function's
        # own signature hardcodes `weights_only=False`. `_is_weights_only_rejection`
        # above closes the ACCIDENT case (an already-held file whose
        # format has drifted past today's allowlist); this closes the
        # ATTACK case (a file we fetch fresh) by routing `loader()`
        # through `_load_hub_download_with_forced_weights_only`, which
        # forces `weights_only=True` on the underlying
        # `torch.hub.load_state_dict_from_url` call for the duration of
        # this one call -- WITHOUT pre-fetching or pre-placing the file
        # ourselves. torch.hub still resolves its own cache path and
        # issues its own request exactly as before, so this does not
        # couple this file to torch.hub's internal cache-filename
        # convention (the reason a pre-placement fix was rejected: it
        # would risk silently breaking offline weight loading, which
        # runs on every ordinary run, to close an attack case that
        # needs a compromised upstream). A class outside today's
        # allowlist is refused loudly, immediately, and by name; an
        # ordinary legitimate checkpoint still loads (see
        # `_load_hub_download_with_forced_weights_only`'s own
        # docstring). ***
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
        # The downloaded file is the same fairseq-style checkpoint as
        # the cached one, so it needs the same allowlist before
        # `loader()` unpickles it (see
        # `_allow_argparse_namespace_in_checkpoints`) -- now load-
        # bearing on this path too, since this step is strict now, not
        # merely harmless-if-present.
        _allow_argparse_namespace_in_checkpoints()
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_DOWNLOAD_ATTEMPTS + 1):
            previous_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(_DOWNLOAD_TIMEOUT_SECONDS)
            try:
                return _load_hub_download_with_forced_weights_only(loader)
            except Exception as exc:  # noqa: BLE001 - translate + sanitize below
                last_exc = exc
                if isinstance(exc, ModelLoadError):
                    # Fail closed: an allowlist rejection on the fresh-
                    # download path is not a network error. It must
                    # never be retried (retrying calls the exact same
                    # refusal again) and must never be sanitized down
                    # to the generic "RNA-FM model unavailable" below --
                    # the caller needs the allowlist name, from
                    # _load_hub_download_with_forced_weights_only's own
                    # message, to know what to update.
                    raise
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
