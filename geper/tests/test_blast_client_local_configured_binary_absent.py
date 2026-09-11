"""Local BLAST configured but `blastn` absent -> refuse at construction, never fall through to remote.

Ruled 2026-09-11 by god under the human's #21 ("ABSENT is a confirmed missing
prerequisite and should kill the run"): after the image trim removed
ncbi-blast+, a site that configured a local BLAST database to keep patient
sequence on-premises was moved to REMOTE BLAST -- the sequence left the site
under an INFO line ("BLAST mode 'auto' resolved to 'remote' (no usable local
blastn/database found ...)") that never says a configured local database was
ignored. Measured 2026-09-11; the card had said it "logs nothing", which is
nearly but not exactly true. "An operator who configured local
BLAST did it on purpose; choosing remote for them is an egress decision
nobody made."

"Configured local" means ANY documented local-database source, not only the
variable the ruling named: the `local_db_path` argument (--blast-db),
GEPER_BLAST_DATABASE (the PRIMARY variable per config.py), GEPER_BLAST_LOCAL_DB,
and NCBI's BLASTDB fallback. Refusing on one of them would leave the others
egressing silently.

Only mode="auto" is refused, because only "auto" falls through to remote.
Explicit mode="remote" is the operator making the egress decision; explicit
mode="local" never leaves the site (it fails at the first search, as before).
Both are pinned below so either choice can be overruled visibly rather than
discovered.

The biopython check is reported PRESENT throughout: otherwise "auto" degrades
to "skip" instead of "remote", and the egress this card is about never arises.
"""

import dataclasses
import os
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from config import CONFIG
from database.blast_client import BLASTClient
from utils.auto_install import PackageCheckStatus
from utils.exceptions import PipelineError

_DB = "/data/blastdb/GRCh38"
_LOCAL_ENV_VARS = ("GEPER_BLAST_DATABASE", "GEPER_BLAST_LOCAL_DB", "BLASTDB")


def _construct(*, env=None, config_db="", blastn=None, db_files=True, **kwargs):
    """Construct a BLASTClient with the local-DB configuration and the
    presence of `blastn` fully controlled, offline.

    Returns (client_or_None, raised_or_None, mocks) where mocks names the
    network-reaching seams so a test can assert they were never touched.
    """
    env = env or {}
    with ExitStack() as stack:
        # Start from NO local-DB configuration from the host box, then apply `env`.
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        for var in _LOCAL_ENV_VARS:
            os.environ.pop(var, None)
        os.environ.update(env)
        # CONFIG is a frozen dataclass: replace, then patch the module's name
        # for it -- the convention test_mmsplice_integration.py uses.
        api = dataclasses.replace(CONFIG.api, BLAST_LOCAL_DB_PATH=config_db, BLAST_REFERENCE_FASTA="")
        stack.enter_context(patch("database.blast_client.CONFIG", dataclasses.replace(CONFIG, api=api)))
        stack.enter_context(
            patch("database.blast_client.shutil.which", side_effect=lambda tool: blastn if tool == "blastn" else None)
        )
        stack.enter_context(patch("database.blast_client._has_local_db_files", return_value=db_files))
        stack.enter_context(patch("database.blast_client.ensure_local_blast_db", return_value=None))
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


class RefusesWhenLocalIsConfiguredAndBlastnIsAbsent(unittest.TestCase):
    def _assert_refused_naming(self, raised, source):
        self.assertIsInstance(raised, PipelineError, f"expected a refusal, got {raised!r}")
        message = str(raised)
        self.assertIn(source, message)
        self.assertIn("blastn", message)

    def test_geper_blast_local_db_is_refused_not_moved_to_remote(self):
        client, raised, _ = _construct(env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, mode="auto")
        self.assertIsNone(
            client, "must not construct -- today it constructs as REMOTE and the sequence leaves the site"
        )
        self._assert_refused_naming(raised, "GEPER_BLAST_LOCAL_DB")

    def test_geper_blast_database_primary_variable_is_refused(self):
        _, raised, _ = _construct(env={"GEPER_BLAST_DATABASE": _DB}, config_db=_DB, mode="auto")
        self._assert_refused_naming(raised, "GEPER_BLAST_DATABASE")

    def test_blastdb_fallback_is_refused(self):
        _, raised, _ = _construct(env={"BLASTDB": _DB}, mode="auto")
        self._assert_refused_naming(raised, "BLASTDB")

    def test_explicit_local_db_path_argument_is_refused(self):
        _, raised, _ = _construct(local_db_path=_DB, mode="auto")
        self._assert_refused_naming(raised, "local_db_path")

    def test_the_refusal_names_the_source_but_never_the_path(self):
        # Round 28 (see blast_client._search_local): what is raised names the
        # failure, never the local filesystem path.
        _, raised, _ = _construct(env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, mode="auto")
        self.assertIsInstance(raised, PipelineError)
        self.assertNotIn(_DB, str(raised))

    def test_the_refusal_path_makes_no_network_call(self):
        _, raised, mocks = _construct(env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, mode="auto")
        self.assertIsInstance(raised, PipelineError)
        for name, mock in mocks.items():
            self.assertFalse(mock.called, f"{name} was called on the refusal path")


class ControlsBehaveExactlyAsToday(unittest.TestCase):
    def test_no_local_db_anywhere_still_resolves_remote(self):
        client, raised, _ = _construct(blastn=None, mode="auto")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")

    def test_local_db_and_blastn_present_still_resolves_local(self):
        client, raised, _ = _construct(
            env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, blastn="/usr/bin/blastn", mode="auto"
        )
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "local")

    def test_explicit_remote_mode_is_the_operators_own_egress_choice(self):
        client, raised, _ = _construct(env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, blastn=None, mode="remote")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "remote")

    def test_explicit_local_mode_is_not_refused_it_never_leaves_the_site(self):
        # Only "auto" falls through to remote. Explicit local without blastn
        # constructs as local and fails at the first search, exactly as
        # before -- no egress, so not this card's refusal.
        client, raised, _ = _construct(local_db_path=_DB, blastn=None, mode="local")
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertEqual(client.mode, "local")

    def test_disabled_client_never_refuses(self):
        client, raised, _ = _construct(
            env={"GEPER_BLAST_LOCAL_DB": _DB}, config_db=_DB, blastn=None, mode="auto", disabled=True
        )
        self.assertIsNone(raised, f"got {raised!r}")
        self.assertTrue(client.disabled)


if __name__ == "__main__":
    unittest.main()
