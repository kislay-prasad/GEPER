"""
Unit tests for models/rna_fm.py's fix for the "HTTP Error 403:
Forbidden" RNA-FM weight-download failure.

The real `fm` package (and any real network access to
proj.cse.cuhk.edu.hk) is mocked throughout -- these tests exercise
GEPER's own cache-detection, retry, and error-sanitization logic, not
the upstream package or a real network call.
"""

import argparse
import pickle
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import torch

from models.rna_fm import (
    RNAFMModel,
    _allow_argparse_namespace_in_checkpoints,
    _cached_weights_path,
    _fetch_from_official_hf_mirror,
    _HF_MIRROR_REPO_ID,
    _is_permanent_download_error,
    _torch_hub_checkpoint_path,
)
from utils.exceptions import ModelLoadError
from utils.auto_install import PackageCheckStatus


def _fake_alphabet():
    alphabet = mock.Mock()
    alphabet.get_batch_converter.return_value = mock.Mock()
    return alphabet


def _fake_model():
    model = mock.Mock()
    model.to.return_value = model
    model.eval.return_value = None
    return model


class TestCacheDetection(unittest.TestCase):
    def test_no_cache_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                self.assertIsNone(_cached_weights_path("rna_fm_t12"))

    def test_cache_hit_when_file_exists(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            weight_file = checkpoints / "RNA-FM_pretrained.pth"
            weight_file.write_bytes(b"not-a-real-checkpoint")

            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                found = _cached_weights_path("rna_fm_t12")
                self.assertEqual(found, weight_file)

    def test_unknown_variant_never_claims_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                self.assertIsNone(_cached_weights_path("some_future_variant"))

    def test_checkpoint_path_matches_torch_hub_convention(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                path = _torch_hub_checkpoint_path("RNA-FM_pretrained.pth")
                self.assertEqual(path, Path(hub_dir) / "checkpoints" / "RNA-FM_pretrained.pth")


class TestPermanentVsTransientErrors(unittest.TestCase):
    def test_http_error_is_permanent(self):
        exc = urllib.error.HTTPError(
            url="https://proj.cse.cuhk.edu.hk/rnafm/api/download?filename=RNA-FM_pretrained.pth",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        self.assertTrue(_is_permanent_download_error(exc))

    def test_plain_url_error_is_not_permanent(self):
        exc = urllib.error.URLError("Connection refused")
        self.assertFalse(_is_permanent_download_error(exc))

    def test_runtime_error_is_not_flagged_permanent_but_still_not_retried(self):
        # RuntimeError isn't a recognized network error at all (e.g. a
        # corrupt-checkpoint state_dict mismatch) -- it's handled by
        # the "not a network error we retry" branch in
        # _load_pretrained_with_fallback, not by _is_permanent_download_error.
        self.assertFalse(_is_permanent_download_error(RuntimeError("bad state dict")))


class TestOfficialHFMirror(unittest.TestCase):
    """Covers the primary fix: RNA-FM weights should come from the
    authors' own official HF mirror (cuhkaih/rnafm) before ever
    touching the upstream package's unreliable endpoint."""

    def test_unrecognized_variant_returns_none_without_touching_network(self):
        logger = mock.Mock()
        with mock.patch("models.rna_fm.ensure_pip_package_available") as mock_ensure:
            result = _fetch_from_official_hf_mirror("some_future_variant", logger)
        self.assertIsNone(result)
        mock_ensure.assert_not_called()

    def test_huggingface_hub_unavailable_returns_none(self):
        logger = mock.Mock()
        with mock.patch("models.rna_fm.ensure_pip_package_available", return_value=False):
            result = _fetch_from_official_hf_mirror("rna_fm_t12", logger)
        self.assertIsNone(result)

    def test_successful_fetch_copies_file_to_torch_hub_cache_location(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as hub_dir, tempfile.TemporaryDirectory() as hf_cache_dir:
            source_file = Path(hf_cache_dir) / "downloaded.pth"
            source_file.write_bytes(b"real-weights")

            fake_hf_hub = types.ModuleType("huggingface_hub")
            fake_hf_hub.hf_hub_download = mock.Mock(return_value=str(source_file))

            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm.ensure_pip_package_available", return_value=True),
                mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf_hub}),
            ):
                result = _fetch_from_official_hf_mirror("rna_fm_t12", logger)

            expected_dest = Path(hub_dir) / "checkpoints" / "RNA-FM_pretrained.pth"
            self.assertEqual(result, expected_dest)
            self.assertTrue(expected_dest.is_file())
            self.assertEqual(expected_dest.read_bytes(), b"real-weights")
            fake_hf_hub.hf_hub_download.assert_called_once_with(
                repo_id=_HF_MIRROR_REPO_ID,
                filename="RNA-FM_pretrained.pth",
                etag_timeout=mock.ANY,
            )

    def test_network_failure_returns_none_and_is_never_fatal(self):
        logger = mock.Mock()
        fake_hf_hub = types.ModuleType("huggingface_hub")
        fake_hf_hub.hf_hub_download = mock.Mock(side_effect=OSError("network unreachable"))

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm.ensure_pip_package_available", return_value=True),
                mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf_hub}),
            ):
                result = _fetch_from_official_hf_mirror("rna_fm_t12", logger)

        self.assertIsNone(result)

    def test_load_pretrained_prefers_mirror_over_upstream_endpoint(self):
        """The primary end-to-end behavior change: when nothing is
        cached yet, a successful mirror fetch must be used, and the
        upstream (unreliable) endpoint must never be called at all."""
        instance = RNAFMModel.__new__(RNAFMModel)
        instance.logger = mock.Mock()
        instance.device = "cpu"

        good_result = (_fake_model(), _fake_alphabet())
        loader = mock.Mock(return_value=good_result)

        with tempfile.TemporaryDirectory() as hub_dir:
            mirror_path = Path(hub_dir) / "checkpoints" / "RNA-FM_pretrained.pth"
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch(
                    "models.rna_fm._fetch_from_official_hf_mirror",
                    return_value=mirror_path,
                ),
            ):
                result = instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(result, good_result)
        loader.assert_called_once_with(model_location=str(mirror_path))

    def test_mirror_load_failure_falls_back_to_upstream_endpoint(self):
        instance = RNAFMModel.__new__(RNAFMModel)
        instance.logger = mock.Mock()
        instance.device = "cpu"

        good_result = (_fake_model(), _fake_alphabet())

        def loader(model_location=None):
            if model_location is not None:
                raise RuntimeError("corrupt mirrored checkpoint")
            return good_result

        with tempfile.TemporaryDirectory() as hub_dir:
            mirror_path = Path(hub_dir) / "checkpoints" / "RNA-FM_pretrained.pth"
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch(
                    "models.rna_fm._fetch_from_official_hf_mirror",
                    return_value=mirror_path,
                ),
            ):
                result = instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(result, good_result)


class TestRNAFMLoadFallback(unittest.TestCase):
    def setUp(self):
        self.instance = RNAFMModel.__new__(RNAFMModel)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"

    def test_uses_cached_weights_without_network_call(self):
        loader = mock.Mock(return_value=(_fake_model(), _fake_alphabet()))
        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            weight_file = checkpoints / "RNA-FM_pretrained.pth"
            weight_file.write_bytes(b"cached")

            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                model, alphabet = self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertIsNotNone(model)
        self.assertIsNotNone(alphabet)
        loader.assert_called_once_with(model_location=str(weight_file))

    def test_corrupted_cache_falls_back_to_network_attempt(self):
        good_result = (_fake_model(), _fake_alphabet())

        def loader(model_location=None):
            if model_location is not None:
                raise RuntimeError("corrupt checkpoint")
            return good_result

        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            (checkpoints / "RNA-FM_pretrained.pth").write_bytes(b"corrupt")

            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                result = self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(result, good_result)


class _UnlistedCheckpointField:
    """Module-level so pickle can resolve it by qualified name. Stands
    in for ANY class an ordinary future/different RNA-FM checkpoint
    revision might carry that today's allowlist does not yet cover --
    the point is only that it is not argparse.Namespace and not on any
    default-safe list, not what it specifically represents. See
    TestAllowArgparseNamespaceIsLoadBearing's docstring for the same
    "synthetic stands in for the real checkpoint" reasoning."""

    def __init__(self, value):
        self.value = value


def _real_fm_style_loader(hub_dir):
    """Reproduces `fm.pretrained.rna_fm_t12`'s own two real code paths
    (fm/pretrained.py::load_model_and_alphabet_local /
    load_hub_workaround) with REAL torch calls -- `torch.load` with no
    `weights_only` kwarg for the local path (torch's own, stricter
    2.6+ default), and REAL `torch.hub.load_state_dict_from_url` for
    the no-`model_location` path, whose own signature hardcodes
    `weights_only=False`. Never mocked, and needs no `fm` package
    install -- the file is placed at the exact path
    `torch.hub.get_dir()`-based resolution expects, so
    `load_state_dict_from_url` finds it already cached and never
    touches a network, real or fake."""

    def loader(model_location=None):
        if model_location is not None:
            return torch.load(str(model_location), map_location="cpu")
        return torch.hub.load_state_dict_from_url(
            "https://example.invalid/RNA-FM_pretrained.pth",
            model_dir=str(Path(hub_dir) / "checkpoints"),
            file_name="RNA-FM_pretrained.pth",
            map_location="cpu",
        )

    return loader


class TestFallbackDoesNotMaskAnAllowlistGap(unittest.TestCase):
    """HIGH-priority card: `_load_pretrained_with_fallback`'s cache-hit
    and HF-mirror branches must not classify a `weights_only` rejection
    (an allowlist gap on a file we ALREADY hold -- ordinary upstream
    checkpoint-format drift, not an attack and not corruption) as
    "corrupted, try the wider path" -- doing so silently re-reads the
    SAME bytes through `torch.hub.load_state_dict_from_url`'s own
    `weights_only=False` default.

    Uses `_real_fm_style_loader` throughout -- REAL `torch.load` and
    REAL `torch.hub.load_state_dict_from_url`, never mocked. A mocked
    assertion here would reproduce the exact blindness of
    `TestRNAFMLoadFallback` above (whose `loader` stand-ins are plain
    `mock.Mock`/closures returning canned results), which is how this
    survived undetected.

    RESIDUAL, NOT CLOSED BY THIS FILE: a GENUINE FRESH network download
    carrying the same kind of unlisted class still succeeds through the
    wide path -- step 2 of `_load_pretrained_with_fallback` still calls
    `loader()` -> `torch.hub.load_state_dict_from_url`'s own
    `weights_only=False` default, unchanged. This class closes the
    ACCIDENT case (drift on a file we already hold); it does not close
    the ATTACK case (a file we fetch fresh). See
    `_load_pretrained_with_fallback`'s own comment at step 2 for where
    that residual is named in the code.
    """

    def setUp(self):
        self.instance = RNAFMModel.__new__(RNAFMModel)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"

    def test_cache_hit_allowlist_gap_raises_loudly_and_never_reaches_the_wide_path(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            weight_file = checkpoints / "RNA-FM_pretrained.pth"
            torch.save(
                {"args": argparse.Namespace(arch="test_arch"), "extra_field": _UnlistedCheckpointField(1)},
                weight_file,
            )

            wide_path_called = []
            real_loader = _real_fm_style_loader(hub_dir)

            def spying_loader(model_location=None):
                if model_location is None:
                    wide_path_called.append(True)
                return real_loader(model_location=model_location)

            with mock.patch("torch.hub.get_dir", return_value=hub_dir):
                with self.assertRaises(ModelLoadError) as ctx:
                    self.instance._load_pretrained_with_fallback(spying_loader, "rna_fm_t12")

        message = str(ctx.exception)
        self.assertIn("allowlist", message.lower())
        self.assertIn("not corruption", message.lower())  # must say so explicitly, not just omit the word
        self.assertEqual(
            wide_path_called,
            [],
            "the wide (weights_only=False) path must never be attempted once the local load "
            "is recognised as an allowlist gap rather than corruption",
        )

    def test_hf_mirror_allowlist_gap_gets_the_same_treatment(self):
        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            # Real `_fetch_from_official_hf_mirror` writes to exactly
            # this canonical path (`_torch_hub_checkpoint_path`) and
            # returns it -- reproduced here as a side_effect (not a
            # pre-placed file + return_value) so `_cached_weights_path`
            # genuinely sees nothing at the start of the call (forcing
            # the HF-mirror branch) and the file only appears at the
            # moment the real function would have "downloaded" it.
            weight_file = checkpoints / "RNA-FM_pretrained.pth"

            def fake_mirror_fetch(model_variant, logger):
                torch.save(
                    {"args": argparse.Namespace(arch="test_arch"), "extra_field": _UnlistedCheckpointField(1)},
                    weight_file,
                )
                return weight_file

            wide_path_called = []
            real_loader = _real_fm_style_loader(hub_dir)

            def spying_loader(model_location=None):
                if model_location is None:
                    wide_path_called.append(True)
                return real_loader(model_location=model_location)

            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", side_effect=fake_mirror_fetch),
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    self.instance._load_pretrained_with_fallback(spying_loader, "rna_fm_t12")

        message = str(ctx.exception)
        self.assertIn("allowlist", message.lower())
        self.assertEqual(wide_path_called, [])

    def test_genuinely_corrupted_cached_file_is_quarantined_before_the_retry(self):
        """Honest corruption, not a fake exception: a real torch
        checkpoint truncated mid-archive -- measured (this file's own
        setup below) to raise a real, non-UnpicklingError exception in
        the pinned torch version, i.e. NOT the allowlist-gap shape --
        so this must still fall through to the retry, but must not
        leave the bad file sitting where the retry (or the cache-check
        on the NEXT load) would find and silently re-read it."""
        with tempfile.TemporaryDirectory() as hub_dir:
            checkpoints = Path(hub_dir) / "checkpoints"
            checkpoints.mkdir(parents=True)
            weight_file = checkpoints / "RNA-FM_pretrained.pth"
            good = checkpoints / "good.pt"
            torch.save({"a": torch.zeros(10)}, good)
            data = good.read_bytes()
            weight_file.write_bytes(data[: len(data) // 2])  # genuinely truncated, not a mock
            good.unlink()

            def loader(model_location=None):
                if model_location is not None:
                    return torch.load(str(model_location), map_location="cpu")  # real load, real corruption
                raise urllib.error.HTTPError(
                    url="https://proj.cse.cuhk.edu.hk/x", code=404, msg="Not Found", hdrs=None, fp=None
                )

            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
            ):
                with self.assertRaises(ModelLoadError):
                    self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

            self.assertFalse(
                weight_file.exists(),
                "the corrupted file must be moved aside, not left for a later retry/load to re-read",
            )
            quarantined = list(checkpoints.glob("RNA-FM_pretrained.pth.corrupted-*"))
            self.assertEqual(len(quarantined), 1, f"expected exactly one quarantined file, found {quarantined}")

    def test_http_403_is_sanitized_and_not_retried(self):
        http_403 = urllib.error.HTTPError(
            url="https://proj.cse.cuhk.edu.hk/rnafm/api/download?filename=RNA-FM_pretrained.pth",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        loader = mock.Mock(side_effect=http_403)

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
                mock.patch("time.sleep") as mock_sleep,
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        message = str(ctx.exception)
        self.assertNotIn("403", message)
        self.assertNotIn("Forbidden", message)
        self.assertNotIn("proj.cse.cuhk.edu.hk", message)
        self.assertEqual(message, "RNA-FM model unavailable")
        # A definite HTTP error must not be retried at all.
        loader.assert_called_once()
        mock_sleep.assert_not_called()

    def test_transient_error_is_retried_then_gracefully_skipped(self):
        transient = urllib.error.URLError("Connection reset by peer")
        loader = mock.Mock(side_effect=transient)

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
                mock.patch("time.sleep") as mock_sleep,
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(str(ctx.exception), "RNA-FM model unavailable")
        self.assertEqual(loader.call_count, 3)  # _MAX_DOWNLOAD_ATTEMPTS
        self.assertEqual(mock_sleep.call_count, 2)

    def test_transient_error_recovers_on_retry(self):
        good_result = (_fake_model(), _fake_alphabet())
        transient = urllib.error.URLError("temporary DNS failure")
        loader = mock.Mock(side_effect=[transient, good_result])

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
                mock.patch("time.sleep") as mock_sleep,
            ):
                result = self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(result, good_result)
        self.assertEqual(loader.call_count, 2)
        mock_sleep.assert_called_once()

    def test_unrecognized_error_is_not_retried(self):
        loader = mock.Mock(side_effect=RuntimeError("Error(s) in loading state_dict"))

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
                mock.patch("time.sleep") as mock_sleep,
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(str(ctx.exception), "RNA-FM model unavailable")
        loader.assert_called_once()
        mock_sleep.assert_not_called()


class TestRNAFMLoadImplIntegration(unittest.TestCase):
    """Exercises _load_impl end to end with a fully mocked `fm` module,
    confirming the sanitized failure propagates the way
    pipeline/orchestrator.py's per-variant RNA stage consumes it
    (`reason: str(exc)`), i.e. that a clinical report would render
    'Skipped: RNA-FM model unavailable.' -- never raw HTTP/network
    text."""

    def setUp(self):
        self.fake_fm = types.ModuleType("fm")
        self.fake_fm.pretrained = types.SimpleNamespace()
        self.patched_modules = mock.patch.dict(sys.modules, {"fm": self.fake_fm})
        self.patched_modules.start()
        self.addCleanup(self.patched_modules.stop)

    def test_load_impl_skips_gracefully_on_403(self):
        http_403 = urllib.error.HTTPError(
            url="https://proj.cse.cuhk.edu.hk/rnafm/api/download?filename=RNA-FM_pretrained.pth",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        self.fake_fm.pretrained.rna_fm_t12 = mock.Mock(side_effect=http_403)

        instance = RNAFMModel.__new__(RNAFMModel)
        instance.logger = mock.Mock()
        instance.device = "cpu"

        with tempfile.TemporaryDirectory() as hub_dir:
            with (
                mock.patch("torch.hub.get_dir", return_value=hub_dir),
                mock.patch("models.rna_fm.check_rna_fm_availability", return_value=PackageCheckStatus.PRESENT),
                mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None),
                mock.patch("time.sleep"),
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    instance._load_impl()

        message = str(ctx.exception)
        self.assertEqual(message, "RNA-FM model unavailable")
        for forbidden_substring in ("403", "Forbidden", "cuhk", "HTTP Error"):
            self.assertNotIn(forbidden_substring, message)


def _write_synthetic_namespace_checkpoint(path):
    """A minimal, offline stand-in for the real ~1.2GB fairseq-style RNA-FM
    checkpoint: a plain dict holding an `argparse.Namespace` (the exact
    class the real checkpoint trips PyTorch 2.6+'s stricter unpickler
    with, at `.args` and `.cfg.model` -- see
    `_allow_argparse_namespace_in_checkpoints`'s own docstring). Real
    `torch.save`/`torch.load`, real `argparse.Namespace` -- only the
    checkpoint's SIZE and the real `rna-fm` package's model-construction
    logic are stood in for."""
    torch.save({"args": argparse.Namespace(arch="test_arch", foo=1), "model": {}}, path)


class TestAllowArgparseNamespaceIsLoadBearing(unittest.TestCase):
    """Negative control for RNA-FM's `argparse.Namespace` allowlist
    (c636943): proves `_allow_argparse_namespace_in_checkpoints` is
    load-bearing rather than decorative -- the exact question this card
    asked, and the exact method (Kelly's, for HyenaDNA/6234ffa) god
    required: neutralise the call (never make it) and confirm the load
    fails with the SPECIFIC error the allowlist exists to prevent, then
    confirm calling it makes the identical file load.

    Offline, no `fm` package or real weights needed -- see this card's
    report for the additional live confirmation against the real,
    ~1.2GB cached checkpoint in geper:bridge-ready
    (sha256:a8a5fe67749e...) under `--network none`, which this test
    cannot substitute for (a synthetic Namespace proves the MECHANISM;
    only the real file proves the real checkpoint still needs it).

    `torch.serialization` safe-globals state is process-global and
    order-dependent across the whole test session -- saved and restored
    around every test here so this file cannot leak a permissive state
    into (or inherit a stale one from) any other test.
    """

    def setUp(self):
        self._saved_safe_globals = torch.serialization.get_safe_globals()
        torch.serialization.clear_safe_globals()

    def tearDown(self):
        torch.serialization.clear_safe_globals()
        if self._saved_safe_globals:
            torch.serialization.add_safe_globals(self._saved_safe_globals)

    def test_without_the_allowlist_call_loading_fails_with_the_right_error(self):
        """THE NEGATIVE CONTROL. `argparse.Namespace` is deliberately left
        un-allowlisted (setUp already cleared it, and this test never
        calls `_allow_argparse_namespace_in_checkpoints`)."""
        with tempfile.TemporaryDirectory() as d:
            ckpt_path = Path(d) / "synthetic_checkpoint.pt"
            _write_synthetic_namespace_checkpoint(ckpt_path)

            with self.assertRaises(pickle.UnpicklingError) as ctx:
                torch.load(str(ckpt_path))  # weights_only left at torch's own default

        message = str(ctx.exception)
        self.assertTrue(
            "argparse.Namespace" in message or "Unsupported global" in message,
            f"the negative control failed for the WRONG reason -- got: {message!r}. "
            "It must fail specifically because argparse.Namespace is not "
            "allowlisted (a corrupt/missing file, or any other failure, would "
            "not prove the allowlist is what matters here).",
        )

    def test_calling_the_real_function_makes_the_identical_file_load(self):
        """THE CONTROL. Same synthetic file, same process -- only
        difference is calling the real, unmodified production function
        first."""
        with tempfile.TemporaryDirectory() as d:
            ckpt_path = Path(d) / "synthetic_checkpoint.pt"
            _write_synthetic_namespace_checkpoint(ckpt_path)

            _allow_argparse_namespace_in_checkpoints()
            loaded = torch.load(str(ckpt_path))

        self.assertIsInstance(loaded["args"], argparse.Namespace)
        self.assertEqual(loaded["args"].arch, "test_arch")

    def test_torch_load_is_not_monkeypatched_by_the_allowlist_call(self):
        """`add_safe_globals` must register a permitted class, never
        replace `torch.load` itself -- a monkeypatch here would silently
        reintroduce the exact blanket-`weights_only=False` risk c636943
        replaced, just one level further from view."""
        original_torch_load = torch.load
        _allow_argparse_namespace_in_checkpoints()
        self.assertIs(torch.load, original_torch_load)


if __name__ == "__main__":
    unittest.main()
