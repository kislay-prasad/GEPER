"""
Unit tests for utils/auto_install.py.

Focuses on ensure_pip_package_available's --constraint safety net (see
its own docstring / _REQUIREMENTS_CONSTRAINT_PATH for the full story):
confirms the pip command always carries --constraint pointed at
requirements.txt, and that a package pip refuses because of that
constraint returns False cleanly -- via the existing non-zero-exit
handling, no new failure logic -- without ever reporting the package
as installed. Everything here is mocked at the subprocess boundary, so
it needs no real pip invocation, no network, and no GPU.
"""

import unittest
from unittest import mock

from utils import auto_install
from utils.auto_install import _REQUIREMENTS_CONSTRAINT_PATH, ensure_pip_package_available


class TestEnsurePipPackageAvailableConstraint(unittest.TestCase):
    def setUp(self):
        # ensure_pip_package_available caches its outcome per pip_name
        # in a module-level dict so a package that fails once doesn't
        # retry on every call within the same process -- clear it so
        # each test starts from a clean slate regardless of order.
        auto_install._INSTALL_RESULTS.clear()

    def tearDown(self):
        auto_install._INSTALL_RESULTS.clear()

    @mock.patch("utils.auto_install.subprocess.run")
    @mock.patch("utils.auto_install.is_pip_package_installed")
    def test_install_command_always_carries_constraint(self, mock_installed, mock_run):
        mock_installed.side_effect = [False, True]
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")

        ok = ensure_pip_package_available("some-fictitious-package", import_name="some_fictitious_package")

        self.assertTrue(ok)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--constraint", cmd)
        constraint_index = cmd.index("--constraint")
        self.assertEqual(cmd[constraint_index + 1], _REQUIREMENTS_CONSTRAINT_PATH)
        self.assertIn("some-fictitious-package", cmd)

    @mock.patch("utils.auto_install.subprocess.run")
    @mock.patch("utils.auto_install.is_pip_package_installed")
    def test_constraint_conflict_returns_false_without_disturbing_constrained_package(self, mock_installed, mock_run):
        # Simulates exactly what pip does when an optional package's
        # own pin (e.g. enformer-pytorch's `transformers==4.56.2`)
        # conflicts with something requirements.txt constrains (e.g.
        # `transformers>=5.12.1,<6.0.0`): a clean non-zero exit, no
        # partial install, nothing touched. This is the failure mode
        # --constraint converts the old silent-downgrade bug into.
        mock_installed.return_value = False
        mock_run.return_value = mock.Mock(
            returncode=1,
            stdout="",
            stderr=(
                "ERROR: Cannot install conflicting-package because these "
                "package versions have conflicting dependencies.\n"
                "The conflict is caused by:\n"
                "    conflicting-package 1.0.0 depends on transformers==4.56.2\n"
                "    The user requested (constraint) transformers>=5.12.1,<6.0.0"
            ),
        )

        ok = ensure_pip_package_available("conflicting-package", import_name="conflicting_package")

        self.assertFalse(ok)
        # Exactly one attempt -- a constraint conflict is not an
        # "externally-managed-environment" refusal, so the
        # --break-system-packages retry path must not fire for it.
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--constraint", cmd)

    @mock.patch("utils.auto_install.subprocess.run")
    def test_already_installed_package_never_invokes_pip(self, mock_run):
        with mock.patch("utils.auto_install.is_pip_package_installed", return_value=True):
            ok = ensure_pip_package_available("numpy")

        self.assertTrue(ok)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
