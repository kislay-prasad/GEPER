"""
Unit tests for pipeline/models/mmsplice/loader.py's `_load_impl`
failure-message sanitization (round 22).

Traced: `MMSpliceModel._load_impl()` used to embed local filesystem
paths (`package_dir`, `layers_path`, `h5_path`) directly into the
`ModelLoadError` it raises. That exception is caught by
`pipeline/orchestrator.py`'s startup validation loop (`instance.predict
(dummy_sequence)` triggers a lazy `_load_impl()` call), which stores
`str(exc)[:600]` verbatim in `self._model_stage_errors["mmsplice"]`.
`pipeline/models/status.py::_mmsplice_status` returns that string
as-is as the "reason" for a FAILED MMSplice entry, and
`report/report_generator.py::_render_ai_model_status` renders it into
the "### AI Models" table on EVERY report, unconditionally (that
method's own docstring: never allowed to return empty) -- the same
leak class round 20/21 fixed for SpliceBERT, confirmed reachable here
by tracing the actual call chain, not assumed.

Only the two failure sites reachable BEFORE any TensorFlow import
(`package_dir` unresolved, `layers.py` missing) are exercised directly
here -- both are pure filesystem checks, no model weights or
TensorFlow involved. The two sites past that point (a missing .h5
weight file, a generic Keras-load exception) apply the identical
fix-pattern (full detail to `self.logger.warning(..., exc_info=True)`,
a sanitized bare/filename-only message raised) but are not separately
exercised here to avoid forcing a TensorFlow import for marginal
additional coverage on an 8GB machine -- verified by code review
instead, matching this project's own established discipline (see
tests/test_splicebert_loader_live.py's docstring for the same
reasoning applied to a different model).
"""

import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline.models.mmsplice.loader import CONFIG, MMSpliceModel
from pipeline.models.mmsplice.models import MODEL_FILENAMES, MODULE_NAMES
from utils.exceptions import ModelLoadError
from utils.auto_install import PackageCheckStatus


class TestMMSpliceLoadImplSanitization(unittest.TestCase):
    def _instance(self):
        instance = MMSpliceModel.__new__(MMSpliceModel)
        instance.logger = mock.Mock()
        instance._keras_models = {}
        return instance

    def test_unresolved_package_dir_does_not_leak_the_resolved_value(self):
        instance = self._instance()
        with (
            mock.patch(
                "pipeline.models.mmsplice.loader.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.mmsplice.loader._resolve_mmsplice_package_dir",
                return_value="/some/local/path/site-packages/mmsplice",
            ),
        ):
            with self.assertRaises(ModelLoadError) as ctx:
                instance._load_impl()

        message = str(ctx.exception)
        self.assertNotIn("/some/local/path", message)
        self.assertIn("GEPER_MMSPLICE_MODEL_DIR", message)
        # Full detail (including the resolved path) must still reach
        # the log, at warning level so a real run captures it.
        instance.logger.warning.assert_called_once()
        self.assertIn("/some/local/path", instance.logger.warning.call_args[0][0])

    def test_none_package_dir_does_not_leak_repr_of_none(self):
        instance = self._instance()
        with (
            mock.patch(
                "pipeline.models.mmsplice.loader.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch("pipeline.models.mmsplice.loader._resolve_mmsplice_package_dir", return_value=None),
        ):
            with self.assertRaises(ModelLoadError) as ctx:
                instance._load_impl()

        self.assertEqual(
            str(ctx.exception),
            "Could not resolve the installed 'mmsplice' package directory. Set "
            "GEPER_MMSPLICE_MODEL_DIR to a directory containing layers.py and a "
            "models/ subdirectory with Acceptor.h5, Donor.h5, Exon.h5, Intron3.h5, "
            "Intron5.h5.",
        )

    def test_missing_layers_file_does_not_leak_the_resolved_path(self):
        instance = self._instance()
        with tempfile.TemporaryDirectory() as tmp:
            # `tmp` deliberately has no layers.py inside it.
            with (
                mock.patch(
                    "pipeline.models.mmsplice.loader.check_pip_package_availability",
                    return_value=PackageCheckStatus.PRESENT,
                ),
                mock.patch(
                    "pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available",
                    return_value=PackageCheckStatus.PRESENT,
                ),
                mock.patch("pipeline.models.mmsplice.loader._resolve_mmsplice_package_dir", return_value=tmp),
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    instance._load_impl()

            message = str(ctx.exception)
            self.assertNotIn(tmp, message)
            self.assertNotIn(str(Path(tmp) / "layers.py"), message)
            self.assertIn("layers.py", message)  # the bare filename is still fine, useful context
            instance.logger.warning.assert_called_once()
            self.assertIn(tmp, instance.logger.warning.call_args[0][0])


def _populate_valid_model_dir(root: str) -> None:
    """A GENUINELY complete, importable MODEL_DIR -- real `layers.py`
    source (harmless stub classes, not the real MMSplice source, but a
    real file `_load_module_from_path` actually execs), plus all five
    `.h5` files at the exact filenames (`MODEL_FILENAMES`) `_load_impl`
    itself checks for downstream. Kept in lock-step with that check
    deliberately -- this helper must never claim "complete" for a
    directory `_load_impl` would still reject."""
    with open(os.path.join(root, "layers.py"), "w") as fh:
        fh.write("class ConvDNA:\n    pass\n\n\nclass GlobalAveragePooling1D_Mask0:\n    pass\n")
    models_dir = os.path.join(root, "models")
    os.makedirs(models_dir, exist_ok=True)
    for filename in MODEL_FILENAMES.values():
        open(os.path.join(models_dir, filename), "wb").close()


def _fake_tensorflow_modules():
    """A minimal, real (not `mock.Mock()`-typed) fake `tensorflow`
    package tree, injected via `sys.modules` -- NOT `mock.patch` on an
    attribute, because `tensorflow` genuinely is not installed in this
    environment (verified: `is_pip_package_installed('tensorflow')` is
    False here) and must not be, per this card's own boundary against
    mutating a shared environment currently held as evidence on another
    card. This lets `_load_impl`'s own real `import tensorflow as tf` /
    `from tensorflow.keras.models import load_model` succeed against a
    stand-in, so the MODEL_DIR-complete test below exercises the REAL
    Keras-loading call (not a mock of `_load_impl` itself), proving the
    model genuinely loads -- not merely that no MODEL_DIR-related error
    was raised."""
    load_model = mock.Mock(side_effect=lambda h5_path, **kw: mock.Mock(name=f"keras_model:{h5_path}"))

    models_module = types.ModuleType("tensorflow.keras.models")
    models_module.load_model = load_model

    keras_module = types.ModuleType("tensorflow.keras")
    keras_module.__path__ = []
    keras_module.models = models_module

    config_module = types.ModuleType("tensorflow.config")
    config_module.list_physical_devices = mock.Mock(return_value=[])

    tf_module = types.ModuleType("tensorflow")
    tf_module.__path__ = []
    tf_module.keras = keras_module
    tf_module.config = config_module

    @contextlib.contextmanager
    def fake_device(_name):
        yield

    tf_module.device = fake_device

    return tf_module, keras_module, models_module, load_model


class TestModelDirNeverTouchesTheNetwork(unittest.TestCase):
    """HIGH-priority defect, human-ruled 2026-09-10: GEPER_MMSPLICE_MODEL_DIR
    is a declaration of intent by an operator who knows their host is
    air-gapped. Once set, MMSplice must NEVER attempt a pip install --
    complete or not:
      - MODEL_DIR set and COMPLETE (layers.py + all five .h5, the exact
        files `_load_impl` itself already checks at :289-317) -> loads,
        no network, ever.
      - MODEL_DIR set and INCOMPLETE -> fails immediately, and the error
        must NAME `GEPER_MMSPLICE_MODEL_DIR` and the specific missing
        file(s) -- never the generic "could not be installed
        automatically", which is misleading once a network attempt was
        never even the right next step.
      - MODEL_DIR unset -> today's behaviour, unchanged (covered by the
        pre-existing tests above).

    Follows the RNA-FM `_cached_weights_path` precedent: check local
    first, rather than failing into a local check only after a network
    attempt already failed.

    Does not install or uninstall anything in the shared environment --
    verified `tensorflow`/`mmsplice` are both already absent here
    (read-only check), and every test below either fakes `tensorflow`
    via `sys.modules` injection or never imports it at all.
    """

    def _instance(self):
        instance = MMSpliceModel.__new__(MMSpliceModel)
        instance.logger = mock.Mock()
        instance._keras_models = {}
        return instance

    def _set_model_dir(self, value):
        original = CONFIG.mmsplice.MODEL_DIR
        object.__setattr__(CONFIG.mmsplice, "MODEL_DIR", value)
        self.addCleanup(lambda: object.__setattr__(CONFIG.mmsplice, "MODEL_DIR", original))

    def test_complete_model_dir_loads_without_ever_calling_the_pip_install_path(self):
        """THE POSITIVE CASE. A spy on `_ensure_mmsplice_package_files_
        available` (the function that shells out to pip) proves the
        network path is never even attempted -- not inferred from the
        outcome alone, same discipline as the RNA-FM allowlist-gap fix.
        `tensorflow` is genuinely never installed in this environment;
        faked via sys.modules so this exercises the real Keras-loading
        call rather than mocking `_load_impl` itself."""
        instance = self._instance()
        with tempfile.TemporaryDirectory() as root:
            _populate_valid_model_dir(root)
            self._set_model_dir(root)
            tf_module, keras_module, models_module, load_model = _fake_tensorflow_modules()

            with (
                mock.patch(
                    "pipeline.models.mmsplice.loader.check_pip_package_availability",
                    return_value=PackageCheckStatus.PRESENT,
                ),
                mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available") as mock_ensure,
                mock.patch.dict(
                    sys.modules,
                    {
                        "tensorflow": tf_module,
                        "tensorflow.keras": keras_module,
                        "tensorflow.keras.models": models_module,
                    },
                ),
            ):
                instance._load_impl()

            mock_ensure.assert_not_called()

        self.assertEqual(set(instance._keras_models), set(MODULE_NAMES))
        self.assertIs(instance.model, instance._keras_models)
        self.assertEqual(load_model.call_count, len(MODEL_FILENAMES))

    def test_incomplete_model_dir_fails_immediately_naming_model_dir_and_the_missing_file(self):
        """THE NEGATIVE CASE, and the one that proves this isn't just a
        looser check that moves the failure without improving the
        message: MODEL_DIR set but missing one required file must fail
        WITHOUT any pip/network attempt (spied, not inferred), and the
        raised error must name both `GEPER_MMSPLICE_MODEL_DIR` and the
        specific missing filename."""
        instance = self._instance()
        with tempfile.TemporaryDirectory() as root:
            _populate_valid_model_dir(root)
            os.remove(os.path.join(root, "models", "Donor.h5"))  # the one file this MODEL_DIR lacks
            self._set_model_dir(root)

            with (
                mock.patch(
                    "pipeline.models.mmsplice.loader.check_pip_package_availability",
                    return_value=PackageCheckStatus.PRESENT,
                ),
                mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available") as mock_ensure,
            ):
                with self.assertRaises(ModelLoadError) as ctx:
                    instance._load_impl()

            mock_ensure.assert_not_called()

        message = str(ctx.exception)
        self.assertIn("GEPER_MMSPLICE_MODEL_DIR", message)
        self.assertIn("Donor.h5", message)

    def test_model_dir_unset_keeps_todays_pip_fallback_behaviour(self):
        """The unset case must be untouched: `_ensure_mmsplice_package_files_
        available` (the pip path) is still consulted exactly as before."""
        instance = self._instance()
        self._set_model_dir("")
        with (
            mock.patch(
                "pipeline.models.mmsplice.loader.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available",
                return_value=PackageCheckStatus.ABSENT,
            ) as mock_ensure,
        ):
            with self.assertRaises(ModelLoadError) as ctx:
                instance._load_impl()

        mock_ensure.assert_called_once()
        self.assertIn("could not be installed automatically", str(ctx.exception))

    def test_is_available_reflects_a_complete_model_dir_without_calling_pip(self):
        with tempfile.TemporaryDirectory() as root:
            _populate_valid_model_dir(root)
            self._set_model_dir(root)
            with mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available") as mock_ensure:
                with mock.patch("pipeline.models.mmsplice.loader.ensure_pip_package_available", return_value=True):
                    self.assertTrue(MMSpliceModel.is_available())
        mock_ensure.assert_not_called()

    def test_is_available_reflects_an_incomplete_model_dir_without_calling_pip(self):
        with tempfile.TemporaryDirectory() as root:
            _populate_valid_model_dir(root)
            os.remove(os.path.join(root, "layers.py"))
            self._set_model_dir(root)
            with mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available") as mock_ensure:
                with mock.patch("pipeline.models.mmsplice.loader.ensure_pip_package_available", return_value=True):
                    self.assertFalse(MMSpliceModel.is_available())
        mock_ensure.assert_not_called()

    def test_unavailability_reason_names_model_dir_not_a_pip_failure(self):
        """Same accuracy principle as the raised ModelLoadError: a reader
        of `unavailability_reason()` (surfaced in startup status/reports)
        must not be told a pip install was attempted or failed when
        MODEL_DIR being set means one never was."""
        with tempfile.TemporaryDirectory() as root:
            _populate_valid_model_dir(root)
            os.remove(os.path.join(root, "models", "Exon.h5"))
            self._set_model_dir(root)
            with mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available") as mock_ensure:
                with mock.patch("pipeline.models.mmsplice.loader.check_pip_package_availability") as mock_tf:
                    mock_tf.return_value = PackageCheckStatus.PRESENT
                    reason = MMSpliceModel.unavailability_reason()

        mock_ensure.assert_not_called()
        self.assertIn("GEPER_MMSPLICE_MODEL_DIR", reason)
        self.assertIn("Exon.h5", reason)
        self.assertNotIn("pip", reason.lower())


if __name__ == "__main__":
    unittest.main()
