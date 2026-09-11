"""Remote-mode BLASTClient construction fails fast on ABSENT Biopython, and only on ABSENT.

Ruled 2026-09-11 (card
DEFECT-blast_client-457-DISCARDS-THE-RESULT-OF-A-CHECK-WHOSE-OWN-COMMENT-SAYS-IT-EXISTS-TO-FAIL-FAST,
option C): "NOT_CHECKED is an honest unknown and shouldn't kill a run; ABSENT is a
confirmed missing prerequisite and should."

Before this file nothing in the suite constructed remote-mode BLAST at all, which is
how a check whose result was discarded survived under a comment promising fail-fast.

The availability check is patched at BOTH names it can be reached through -- the
binding blast_client imported, and the one `ensure_pip_package_available` calls inside
utils.auto_install -- so these tests pin the behaviour ("Biopython is reported as X"),
not which of the two functions the constructor happens to call.

The two does-not-raise tests also assert the check was CONSULTED. Without that they
could not fail: a constructor that never looked at Biopython would pass them too.
"""

import unittest
from unittest.mock import patch

from database.blast_client import BLASTClient
from utils.auto_install import PackageCheckStatus
from utils.exceptions import PipelineError


def _construct_remote_with_biopython(status):
    """Construct BLASTClient(mode="remote") with Biopython reported as `status`.

    Returns (client_or_None, raised_exception_or_None, the check mock).
    """
    with (
        patch("database.blast_client.check_pip_package_availability", return_value=status) as local_check,
        patch("utils.auto_install.check_pip_package_availability", return_value=status) as source_check,
        patch("database.blast_client.ensure_local_blast_db", return_value=None),
    ):
        client, raised = None, None
        try:
            client = BLASTClient(mode="remote", reference_fasta=None, enable_disk_cache=False)
        except Exception as exc:  # noqa: BLE001 -- the test inspects whatever was raised
            raised = exc
        biopython_calls = [
            c for m in (local_check, source_check) for c in m.call_args_list if c.args and c.args[0] == "biopython"
        ]
    return client, raised, biopython_calls


class RemoteConstructionUnderAbsentBiopython(unittest.TestCase):
    def test_absent_raises_at_construction(self):
        client, raised, calls = _construct_remote_with_biopython(PackageCheckStatus.ABSENT)
        self.assertIsNone(client, "construction must not complete when Biopython is confirmed ABSENT")
        self.assertIsInstance(raised, PipelineError)

    def test_absent_message_names_the_package_and_the_failed_attempt(self):
        _, raised, _ = _construct_remote_with_biopython(PackageCheckStatus.ABSENT)
        self.assertIsNotNone(raised)
        message = str(raised)
        self.assertIn("Biopython", message)
        self.assertIn("did not succeed", message)
        # ABSENT means an attempt was made and failed -- it must not be
        # described as never having been looked for.
        self.assertNotIn("not checked", message)
        self.assertNotIn("never looked for", message)


class RemoteConstructionUnderNotCheckedBiopython(unittest.TestCase):
    def test_not_checked_constructs_and_stays_remote(self):
        client, raised, calls = _construct_remote_with_biopython(PackageCheckStatus.NOT_CHECKED)
        self.assertIsNone(raised, f"NOT_CHECKED is an unknown, not a missing prerequisite; got {raised!r}")
        self.assertEqual(client.mode, "remote")
        self.assertTrue(calls, "Biopython's availability was never consulted -- this test could not have failed")


class RemoteConstructionUnderPresentBiopython(unittest.TestCase):
    def test_present_constructs_and_stays_remote(self):
        client, raised, calls = _construct_remote_with_biopython(PackageCheckStatus.PRESENT)
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")
        self.assertTrue(calls, "Biopython's availability was never consulted -- this test could not have failed")


class DisabledRemoteClientDoesNotCheck(unittest.TestCase):
    """AI-only mode (disabled=True) must still construct under ABSENT: BLAST never runs."""

    def test_disabled_remote_constructs_even_when_absent(self):
        with (
            patch("database.blast_client.check_pip_package_availability", return_value=PackageCheckStatus.ABSENT),
            patch("utils.auto_install.check_pip_package_availability", return_value=PackageCheckStatus.ABSENT),
        ):
            client = BLASTClient(mode="remote", reference_fasta=None, enable_disk_cache=False, disabled=True)
        self.assertTrue(client.disabled)


if __name__ == "__main__":
    unittest.main()
