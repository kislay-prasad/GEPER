"""An operator who expressed local-BLAST intent is never silently sent to remote BLAST.

Ruled 2026-09-11 by god under the human's #21: "AN OPERATOR WHO EXPRESSED
LOCAL-BLAST INTENT MUST NEVER BE SILENTLY SENT REMOTE. ONLY EXPLICIT
mode="remote" MAKES THAT EGRESS DECISION." Two sibling paths to the one closed
in 207067b (local DB configured, blastn absent -- see
test_blast_client_local_configured_binary_absent.py):

  (1) local DB configured, blastn PRESENT, but no database files at the
      configured location -- a volume that failed to mount is the likeliest
      real-world trigger, on a site that never trimmed anything;
  (2) only a reference FASTA configured (GEPER_BLAST_REFERENCE_FASTA: "build
      me a local database") and makeblastdb absent, so nothing is built.

Both used to resolve "auto" to REMOTE. Both now refuse at construction.

Case (2) drives the REAL `ensure_local_blast_db` -- it is the function that
quietly returns None and lets "auto" fall through -- against a real FASTA file,
with only `shutil.which` controlled. Every refusal path is asserted to make no
requests call, no Biopython check and no subprocess call.
"""

import dataclasses
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from config import CONFIG
from database.blast_client import BLASTClient
from utils.auto_install import PackageCheckStatus
from utils.exceptions import PipelineError

_DB = "/data/blastdb/GRCh38"
_LOCAL_ENV_VARS = ("GEPER_BLAST_DATABASE", "GEPER_BLAST_LOCAL_DB", "BLASTDB", "GEPER_BLAST_REFERENCE_FASTA")


def _construct(*, tools=(), env=None, config_db="", config_fasta="", db_files=False, **kwargs):
    """Construct a BLASTClient offline, with the local-BLAST configuration and
    which BLAST+ tools are on PATH fully controlled. `tools` names the binaries
    `shutil.which` finds. Returns (client_or_None, raised_or_None, mocks)."""
    env = env or {}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        for var in _LOCAL_ENV_VARS:
            os.environ.pop(var, None)
        os.environ.update(env)
        # CONFIG is frozen: replace, then patch the module's name for it.
        api = dataclasses.replace(CONFIG.api, BLAST_LOCAL_DB_PATH=config_db, BLAST_REFERENCE_FASTA=config_fasta)
        stack.enter_context(patch("database.blast_client.CONFIG", dataclasses.replace(CONFIG, api=api)))
        stack.enter_context(
            patch("database.blast_client.shutil.which", side_effect=lambda t: f"/usr/bin/{t}" if t in tools else None)
        )
        stack.enter_context(patch("database.blast_client._has_local_db_files", return_value=db_files))
        pip_check = stack.enter_context(
            patch("database.blast_client.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT)
        )
        source_check = stack.enter_context(
            patch("utils.auto_install.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT)
        )
        net_get = stack.enter_context(patch("requests.get"))
        net_post = stack.enter_context(patch("requests.post"))
        sub_run = stack.enter_context(patch("subprocess.run", MagicMock()))
        client, raised = None, None
        try:
            client = BLASTClient(enable_disk_cache=False, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- the test inspects whatever was raised
            raised = exc
    mocks = {
        "biopython_check": pip_check,
        "biopython_check_via_auto_install": source_check,
        "requests.get": net_get,
        "requests.post": net_post,
        "subprocess.run": sub_run,
    }
    return client, raised, mocks


class _RefusalAssertions(unittest.TestCase):
    def assert_refused(self, raised, *must_name, mocks=None, not_named=()):
        self.assertIsInstance(raised, PipelineError, f"expected a refusal, got {raised!r}")
        message = str(raised)
        for text in must_name:
            self.assertIn(text, message)
        for text in not_named:
            self.assertNotIn(text, message)
        for name, mock in (mocks or {}).items():
            self.assertFalse(mock.called, f"{name} was called on the refusal path")


class ConfiguredDatabaseMissingWithBlastnPresent(_RefusalAssertions):
    """Sibling (1): blastn is installed, but the configured database is not there."""

    def test_missing_database_files_are_refused_not_moved_to_remote(self):
        client, raised, mocks = _construct(
            tools=("blastn",), env={"GEPER_BLAST_DATABASE": _DB}, config_db=_DB, db_files=False, mode="auto"
        )
        self.assertIsNone(client, "must not construct -- it used to resolve REMOTE")
        # Names the source; never the path (this file's round-28 rule).
        self.assert_refused(raised, "GEPER_BLAST_DATABASE", mocks=mocks, not_named=(_DB,))

    def test_database_present_still_resolves_local(self):
        client, raised, _ = _construct(
            tools=("blastn",), env={"GEPER_BLAST_DATABASE": _DB}, config_db=_DB, db_files=True, mode="auto"
        )
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "local")


class ReferenceFastaConfiguredMakeblastdbAbsent(_RefusalAssertions):
    """Sibling (2): the operator asked for a local database to be BUILT."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fasta = os.path.join(self._tmp.name, "ref.fa")
        with open(self.fasta, "w", encoding="utf-8") as fh:
            fh.write(">chr1\nACGTACGTACGT\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_fasta_env_without_makeblastdb_is_refused_naming_both(self):
        client, raised, mocks = _construct(
            tools=("blastn",),
            env={"GEPER_BLAST_REFERENCE_FASTA": self.fasta},
            config_fasta=self.fasta,
            mode="auto",
        )
        self.assertIsNone(client, "must not construct -- it used to resolve REMOTE")
        self.assert_refused(raised, "GEPER_BLAST_REFERENCE_FASTA", "makeblastdb", mocks=mocks, not_named=(self.fasta,))

    def test_fasta_argument_without_makeblastdb_is_refused(self):
        _, raised, mocks = _construct(tools=(), reference_fasta=self.fasta, mode="auto")
        self.assert_refused(raised, "reference_fasta", "makeblastdb", mocks=mocks, not_named=(self.fasta,))

    def test_fasta_with_an_existing_database_still_resolves_local(self):
        # makeblastdb absent does not matter when the database already exists:
        # ensure_local_blast_db returns it without building.
        client, raised, _ = _construct(tools=("blastn",), reference_fasta=self.fasta, db_files=True, mode="auto")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "local")


class ExplicitRemoteIsUnaffected(unittest.TestCase):
    """Only explicit mode="remote" makes the egress decision -- and it still can."""

    def test_explicit_remote_with_a_missing_configured_database(self):
        client, raised, _ = _construct(
            tools=("blastn",), env={"GEPER_BLAST_DATABASE": _DB}, config_db=_DB, db_files=False, mode="remote"
        )
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")

    def test_explicit_remote_with_a_fasta_and_no_makeblastdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            fasta = os.path.join(tmp, "ref.fa")
            with open(fasta, "w", encoding="utf-8") as fh:
                fh.write(">chr1\nACGT\n")
            client, raised, _ = _construct(tools=(), reference_fasta=fasta, mode="remote")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")

    def test_nothing_local_configured_still_resolves_remote(self):
        client, raised, _ = _construct(tools=("blastn",), mode="auto")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")


if __name__ == "__main__":
    unittest.main()
