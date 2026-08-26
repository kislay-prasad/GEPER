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

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.models.mmsplice.loader import MMSpliceModel
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
            mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available", return_value=True),
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
            mock.patch("pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available", return_value=True),
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
                    "pipeline.models.mmsplice.loader._ensure_mmsplice_package_files_available", return_value=True
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


if __name__ == "__main__":
    unittest.main()
