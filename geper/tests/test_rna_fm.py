"""
Unit tests for models/rna_fm.py's fix for the "HTTP Error 403:
Forbidden" RNA-FM weight-download failure.

The real `fm` package (and any real network access to
proj.cse.cuhk.edu.hk) is mocked throughout -- these tests exercise
GEPER's own cache-detection, retry, and error-sanitization logic, not
the upstream package or a real network call.
"""

import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from models.rna_fm import (
    RNAFMModel,
    _cached_weights_path,
    _fetch_from_official_hf_mirror,
    _HF_MIRROR_REPO_ID,
    _is_permanent_download_error,
    _torch_hub_checkpoint_path,
)
from utils.exceptions import ModelLoadError


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
        with tempfile.TemporaryDirectory() as hub_dir, \
             tempfile.TemporaryDirectory() as hf_cache_dir:
            source_file = Path(hf_cache_dir) / "downloaded.pth"
            source_file.write_bytes(b"real-weights")

            fake_hf_hub = types.ModuleType("huggingface_hub")
            fake_hf_hub.hf_hub_download = mock.Mock(return_value=str(source_file))

            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm.ensure_pip_package_available", return_value=True), \
                 mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf_hub}):
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm.ensure_pip_package_available", return_value=True), \
                 mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf_hub}):
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch(
                     "models.rna_fm._fetch_from_official_hf_mirror",
                     return_value=mirror_path,
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch(
                     "models.rna_fm._fetch_from_official_hf_mirror",
                     return_value=mirror_path,
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None), \
                 mock.patch("time.sleep") as mock_sleep:
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None), \
                 mock.patch("time.sleep") as mock_sleep:
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None), \
                 mock.patch("time.sleep") as mock_sleep:
                result = self.instance._load_pretrained_with_fallback(loader, "rna_fm_t12")

        self.assertEqual(result, good_result)
        self.assertEqual(loader.call_count, 2)
        mock_sleep.assert_called_once()

    def test_unrecognized_error_is_not_retried(self):
        loader = mock.Mock(side_effect=RuntimeError("Error(s) in loading state_dict"))

        with tempfile.TemporaryDirectory() as hub_dir:
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None), \
                 mock.patch("time.sleep") as mock_sleep:
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
            with mock.patch("torch.hub.get_dir", return_value=hub_dir), \
                 mock.patch("models.rna_fm.ensure_rna_fm_available", return_value=True), \
                 mock.patch("models.rna_fm._fetch_from_official_hf_mirror", return_value=None), \
                 mock.patch("time.sleep"):
                with self.assertRaises(ModelLoadError) as ctx:
                    instance._load_impl()

        message = str(ctx.exception)
        self.assertEqual(message, "RNA-FM model unavailable")
        for forbidden_substring in ("403", "Forbidden", "cuhk", "HTTP Error"):
            self.assertNotIn(forbidden_substring, message)


if __name__ == "__main__":
    unittest.main()
