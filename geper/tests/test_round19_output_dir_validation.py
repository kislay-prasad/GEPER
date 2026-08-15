"""
Round 19: `--output-dir` was never verified writable, only created.

A real 51-minute Colab run finished all 6 variants and every stage,
then crashed on the very last line (`JSONResultBuilder.write()`, via
`open(output_path, "w")`) with `FileNotFoundError`. Nothing was
written -- not even an interim checkpoint (only 6 variants ran;
`CONFIG.CHECKPOINT_INTERVAL` defaults to 25, so none ever fired).

VERIFIED AGAINST SOURCE before building anything (the reported premise
was wrong on one count): `pipeline/orchestrator.py::GeperPipeline
.__init__` already called `os.makedirs(self.output_dir, exist_ok=True)`
at startup -- `git log -p -S"os.makedirs(self.output_dir"` shows this
line present since the very first commit (`cb79edc`), not something
missing. So the directory WAS created (or already existed) at process
start. What was genuinely missing is a WRITABILITY check:
`exist_ok=True` only ever confirms the path *exists*, it never
attempts an actual write, so a directory that exists but has lost
write access (or, the likely real cause here: a Google Drive mount
that goes stale mid-run, a well-documented Colab failure mode) passes
`makedirs` silently either way. A startup-time writability probe (this
round's actual fix) turns the "genuinely unwritable/mistyped path from
the start" case into a few-second failure -- it does NOT, and cannot,
protect against a mount going stale forty-five minutes into a run;
that would need a re-check immediately before every write, which is
explicitly NOT what was asked for here (the request was fail-fast at
startup, not write-time resilience).

Checkpoint write path: uses the exact same `JSONResultBuilder.write()`
and the exact same `self.output_dir`-derived `json_path` as the final
write -- same vulnerability, protected by the same startup check,
nothing distinguishes them.

Other write-target path arguments: `--blast-db`'s auto-build
(`ensure_local_blast_db`) and the BLAST disk cache directory
(`_BlastDiskCache.__init__`) are both ALREADY defensive -- both wrap
their filesystem operations in try/except and degrade gracefully
(skip caching / fall back to remote-or-skip) rather than raising.
`--output-dir` was uniquely dangerous because `JSONResultBuilder.write()`
raises uncaught, and is only ever called from an unguarded call site in
`orchestrator.py::run()`'s main loop and post-loop write -- so it is
the one write-target path argument that both fails hard AND fails late.

Everything below is offline and pure -- `utils/output_paths.py` is
deliberately dependency-light (stdlib `os`/`uuid` and
`utils.exceptions` only), so this file never imports
`pipeline.orchestrator` (confirmed elsewhere in this project's own
tests to cost ~218s/275MB via the TensorFlow/absl chain). The claims
about `orchestrator.py`'s and `main.py`'s own call-site wiring are
instead verified as source-text properties (regex-searched line
positions), the same technique `test_round17_run_complete_marker.py`
already established for the same reason.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils.exceptions import PipelineError
from utils.output_paths import ensure_writable_output_dir

_ORCHESTRATOR_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "orchestrator.py"
_MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


class TestEnsureWritableOutputDir(unittest.TestCase):
    def test_creates_missing_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "a", "b", "c")
            self.assertFalse(os.path.isdir(target))
            ensure_writable_output_dir(target)
            self.assertTrue(os.path.isdir(target))

    def test_probe_file_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_writable_output_dir(tmp)
            leftover = [f for f in os.listdir(tmp) if f.startswith(".geper_write_test")]
            self.assertEqual(leftover, [])

    def test_already_existing_writable_directory_is_a_no_op_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_writable_output_dir(tmp)  # does not raise
            ensure_writable_output_dir(tmp)  # calling again also does not raise

    def test_a_file_where_a_directory_is_expected_raises_pipeline_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, "im_a_file_not_a_dir")
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write("x")
            with self.assertRaises(PipelineError) as ctx:
                ensure_writable_output_dir(file_path)
            self.assertIn(file_path, str(ctx.exception))

    def test_unwritable_existing_directory_raises_pipeline_error_with_actionable_message(self):
        # Simulates a directory that exists (makedirs succeeds/no-ops)
        # but this process cannot actually write to (permission bits on
        # POSIX, or -- the real-world case this round is about -- a
        # stale/disconnected mount on Colab). Mocking `open` at the
        # module level rather than relying on OS-specific permission
        # bits, which behave very differently on Windows vs POSIX and
        # would make this test unreliable cross-platform.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("utils.output_paths.open", side_effect=OSError("Read-only file system"), create=True):
                with self.assertRaises(PipelineError) as ctx:
                    ensure_writable_output_dir(tmp)
            message = str(ctx.exception)
            self.assertIn(tmp, message)
            self.assertIn("not writable", message)

    def test_unwritable_directory_error_names_the_real_cause_not_a_generic_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("utils.output_paths.open", side_effect=OSError("Read-only file system"), create=True):
                with self.assertRaises(PipelineError) as ctx:
                    ensure_writable_output_dir(tmp)
            self.assertIn("Read-only file system", str(ctx.exception))


class TestOrchestratorWiresValidationAtStartup(unittest.TestCase):
    """Source-text verification only -- see this file's module
    docstring for why `pipeline.orchestrator` is never imported here."""

    @classmethod
    def setUpClass(cls):
        cls.source = _ORCHESTRATOR_PATH.read_text(encoding="utf-8")

    def test_ensure_writable_output_dir_is_imported(self):
        self.assertIn("from utils.output_paths import ensure_writable_output_dir", self.source)

    def test_ensure_writable_output_dir_is_called_in_init_not_only_in_run(self):
        init_match = re.search(r"def __init__\(", self.source)
        run_match = re.search(r"\n    def run\(\s*\n", self.source)
        call_match = re.search(r"ensure_writable_output_dir\(self\.output_dir\)", self.source)
        assert init_match is not None and run_match is not None and call_match is not None
        self.assertLess(init_match.start(), call_match.start())
        self.assertLess(call_match.start(), run_match.start())

    def test_bare_makedirs_on_output_dir_is_gone(self):
        # The old, insufficient line this round replaces -- must not
        # silently coexist alongside the new check.
        self.assertNotIn("os.makedirs(self.output_dir, exist_ok=True)", self.source)

    def test_checkpoint_write_and_final_write_share_the_same_json_path(self):
        # Confirms the "same problem" claim: both real call sites use
        # the one `json_path` variable, computed once, derived from
        # self.output_dir -- there is exactly one write target for
        # both, protected by exactly one startup check. Counts only
        # actual code lines, not comment lines that reference the same
        # call syntax in prose (e.g. this round's own explanatory
        # comment about the in-loop write, added a few lines above the
        # final write).
        real_call_lines = [
            line
            for line in self.source.splitlines()
            if "result_builder.write(json_path)" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(len(real_call_lines), 2, "expected exactly one in-loop and one final write call")


class TestMainWrapsConstructionInErrorHandling(unittest.TestCase):
    """Source-text verification only -- `main.py` imports
    `pipeline.orchestrator` at module level, so it is never imported
    here either."""

    @classmethod
    def setUpClass(cls):
        cls.source = _MAIN_PATH.read_text(encoding="utf-8")

    def test_pipeline_construction_is_inside_the_try_block(self):
        try_match = re.search(r"\n    try:\n", self.source)
        construct_match = re.search(r"pipeline = GeperPipeline\(", self.source)
        except_match = re.search(r"except PipelineError as exc:", self.source)
        assert try_match is not None and construct_match is not None and except_match is not None
        self.assertLess(try_match.start(), construct_match.start())
        self.assertLess(construct_match.start(), except_match.start())


if __name__ == "__main__":
    unittest.main()
