"""
Pins that the SECOND install seam honours the auto-install pytest guard.

WHY THIS FILE EXISTS. `utils/auto_install.py` has a guard --
`_auto_install_disabled_for_tests()` -- whose entire purpose is that a
live `pip install` must never run mid-test-run. Its own docstring
records why, from a real CI failure: an unpinned install running
mid-run made package availability MUTABLE PROCESS STATE, so test
outcomes depended on execution order and network reachability
(`enformer_pytorch` went ABSENT -> PRESENT inside one pytest process,
CI run 32599323393).

`pipeline/models/mmsplice/loader.py::_ensure_mmsplice_package_files_available`
is a SECOND, INDEPENDENT install seam. It shells out to
`pip install --no-deps mmsplice` to get the bundled `.h5` weights and
`layers.py` onto disk. It never consulted that guard.

IT WAS ONLY EVER SAFE BY ACCIDENT. Both of its callers happen to
short-circuit before reaching it under pytest -- `is_available()` bails
at the tensorflow check, `_load_impl()` raises on NOT_CHECKED first --
and the existing tests that drive `_load_impl` past those points happen
to mock this function too. None of that is a property of the seam. It
is a property of its callers and of every future test author
remembering. The guard belongs IN the seam, which is what these tests
pin.

The subprocess is patched in every test here. A test for "does not
shell out" must never be able to shell out when it regresses.
"""

import unittest
from unittest import mock

from pipeline.models.mmsplice import loader
from utils.auto_install import PackageCheckStatus


class _SeamTestCase(unittest.TestCase):
    def setUp(self):
        # The seam memoises its outcome; a stale entry would mask the
        # behaviour under test.
        loader._NO_DEPS_INSTALL_RESULT.clear()
        self.addCleanup(loader._NO_DEPS_INSTALL_RESULT.clear)


class TestTheNoDepsSeamHonoursThePytestGuard(_SeamTestCase):
    def test_does_not_spawn_pip_when_the_package_is_absent_under_pytest(self):
        """The whole point: no live install mid-test-run."""
        with (
            mock.patch.object(loader, "is_pip_package_installed", return_value=False),
            mock.patch("subprocess.run") as spawned,
        ):
            loader._ensure_mmsplice_package_files_available()
        spawned.assert_not_called()

    def test_reports_not_checked_rather_than_absent_under_pytest(self):
        """
        A skipped check is not a confirmed absence. Reporting ABSENT here
        would let `_load_impl` tell the reader the files "could not be
        installed automatically" when no installation was ever attempted.
        """
        with (
            mock.patch.object(loader, "is_pip_package_installed", return_value=False),
            mock.patch("subprocess.run") as spawned,
        ):
            status = loader._ensure_mmsplice_package_files_available()
        spawned.assert_not_called()
        self.assertIs(status, PackageCheckStatus.NOT_CHECKED)

    def test_a_package_already_present_still_reports_present_under_pytest(self):
        """
        The guard must not blind the seam to a package that IS there.
        `is_pip_package_installed` is a pure importability check -- it
        spawns nothing -- so it stays reachable under pytest.
        """
        with (
            mock.patch.object(loader, "is_pip_package_installed", return_value=True),
            mock.patch("subprocess.run") as spawned,
        ):
            status = loader._ensure_mmsplice_package_files_available()
        spawned.assert_not_called()
        self.assertIs(status, PackageCheckStatus.PRESENT)

    def test_the_skipped_outcome_is_never_memoised(self):
        """
        Caching NOT_CHECKED would make a skipped check look like a
        settled result to every later caller in the process -- the same
        conflation one layer down. `_NO_DEPS_INSTALL_RESULT` exists only
        to avoid re-running the subprocess, and this path never reaches
        the subprocess, so there is nothing to memoise.
        """
        with (
            mock.patch.object(loader, "is_pip_package_installed", return_value=False),
            mock.patch("subprocess.run"),
        ):
            loader._ensure_mmsplice_package_files_available()
        self.assertNotIn("mmsplice", loader._NO_DEPS_INSTALL_RESULT)


class TestTheRealInstallPathIsUnchangedOutsidePytest(_SeamTestCase):
    """
    The guard must not change production behaviour. These force the real
    path the way `test_auto_install.py` does -- by patching the guard
    seam itself -- and assert the subprocess still runs.
    """

    def test_absent_package_still_installs_when_not_under_pytest(self):
        completed = mock.Mock(returncode=0, stderr="")
        with (
            mock.patch.object(loader, "_auto_install_disabled_for_tests", return_value=False),
            mock.patch.object(loader, "is_pip_package_installed", side_effect=[False, True]),
            mock.patch("subprocess.run", return_value=completed) as spawned,
        ):
            status = loader._ensure_mmsplice_package_files_available()
        spawned.assert_called_once()
        self.assertIn("--no-deps", spawned.call_args[0][0])
        self.assertIs(status, PackageCheckStatus.PRESENT)

    def test_a_failed_real_install_reports_absent_not_not_checked(self):
        """ABSENT is only honest once an attempt really happened and really failed."""
        completed = mock.Mock(returncode=1, stderr="boom")
        with (
            mock.patch.object(loader, "_auto_install_disabled_for_tests", return_value=False),
            mock.patch.object(loader, "is_pip_package_installed", return_value=False),
            mock.patch("subprocess.run", return_value=completed) as spawned,
        ):
            status = loader._ensure_mmsplice_package_files_available()
        spawned.assert_called()
        self.assertIs(status, PackageCheckStatus.ABSENT)


class TestUnavailabilityReasonNeverTriggersAnInstall(_SeamTestCase):
    """
    `unavailability_reason()` is a REASON-GETTER. Asking why something is
    unavailable must never make it available -- or try to.

    This is the same third-state distinction one level up, applied to
    this module's own design. Reporting the reason needs to tell "no
    install was ever attempted" apart from "an install was attempted and
    failed", and the tempting way to get that is to call the seam. But
    the seam INSTALLS, and its pytest guard is a property of the
    ENVIRONMENT, not of this function -- so the first caller in a new
    context inherits the trap. The cache is read directly instead: a cold
    cache means nobody has looked, which is true regardless of why.
    """

    def test_asking_for_the_reason_does_not_spawn_pip_outside_pytest(self):
        """The dangerous case: non-pytest context, cold cache."""
        with (
            mock.patch.object(loader, "_auto_install_disabled_for_tests", return_value=False),
            mock.patch.object(loader, "check_pip_package_availability", return_value=PackageCheckStatus.PRESENT),
            mock.patch.object(loader, "is_pip_package_installed", return_value=False),
            mock.patch("subprocess.run") as spawned,
        ):
            reason = loader.MMSpliceModel.unavailability_reason()
        spawned.assert_not_called()
        self.assertIn("not checked", reason)

    def test_a_cold_cache_reports_not_checked_rather_than_install_failed(self):
        """Nobody looked is not the same claim as somebody looked and failed."""
        with (
            mock.patch.object(loader, "check_pip_package_availability", return_value=PackageCheckStatus.PRESENT),
            mock.patch("subprocess.run") as spawned,
        ):
            reason = loader.MMSpliceModel.unavailability_reason()
        spawned.assert_not_called()
        self.assertIn("not checked", reason)
        self.assertNotIn("could not be installed", reason)

    def test_a_warm_cache_holding_a_real_failure_still_says_install_failed(self):
        """
        The precision that IS real must survive. A cached False is the
        outcome of an attempt that happened and failed, so the honest
        reason is still "could not be installed".
        """
        loader._NO_DEPS_INSTALL_RESULT["mmsplice"] = False
        with (
            mock.patch.object(loader, "check_pip_package_availability", return_value=PackageCheckStatus.PRESENT),
            mock.patch("subprocess.run") as spawned,
        ):
            reason = loader.MMSpliceModel.unavailability_reason()
        spawned.assert_not_called()
        self.assertIn("could not be installed", reason)
        self.assertNotIn("not checked", reason)


if __name__ == "__main__":
    unittest.main()
