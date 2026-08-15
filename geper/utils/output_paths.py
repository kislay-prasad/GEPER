"""
Startup-time validation for filesystem paths GEPER writes to over the
course of a run -- specifically `--output-dir`, the one write target
whose own failure mode is both uncaught and late (see
`ensure_writable_output_dir`'s own docstring for the real incident this
exists to prevent).

Deliberately dependency-light (stdlib `os`/`uuid` and
`utils.exceptions` only) so this can be imported and unit-tested
without pulling in `pipeline.orchestrator`'s TensorFlow/absl import
chain (confirmed elsewhere in this project's own tests to cost
~218s/275MB) -- the same "small standalone function" discipline
`pipeline/acmg_rules.py`'s `mtdna_*_skip_result` functions already
established for the same reason.
"""

import os
import uuid

from utils.exceptions import PipelineError


def ensure_writable_output_dir(path: str) -> None:
    """
    Creates `path` (and any missing parent directories) if it doesn't
    exist, then verifies this process can genuinely write to it by
    writing and deleting a small probe file. Raises `PipelineError`
    immediately, with a clear and actionable message, if either step
    fails.

    Round 19: a real 51-minute Colab run finished all 6 variants and
    every stage, then crashed on the very last line
    (`JSONResultBuilder.write()`) with `FileNotFoundError` -- nothing
    was written, not even an interim checkpoint (only 6 variants ran;
    `CONFIG.CHECKPOINT_INTERVAL` defaults to 25, so none ever fired).
    `os.makedirs(path, exist_ok=True)` alone was already called at
    startup before this fix and did not prevent it: the directory
    genuinely existed and was writable at that moment (Drive was
    mounted), so `makedirs` correctly no-opped -- `exist_ok=True` only
    ever confirms the path *exists*, never that it is *currently
    writable*, and the most likely explanation for a real
    `/content/drive/...` path failing only at the very end of a long
    Colab run is the Drive mount going stale mid-run, a class of
    failure this startup-time check cannot detect or prevent by
    itself (nothing can, short of re-checking immediately before every
    write). What this check DOES turn from an hour-long silent trap
    into a few-second, fail-fast rejection is the other, more common
    case: a genuinely unwritable or mistyped `--output-dir` (wrong
    path, read-only permissions, a directory that was never going to
    work) -- exactly the case round 12/17's `run_complete` marker
    could not help with either, since no file is ever written at all
    when this path is wrong from the start.
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise PipelineError(
            f"--output-dir '{path}' could not be created ({exc}). Fix the path or its parent "
            "directory's permissions before rerunning -- this is checked once at startup, before "
            "any model load or network call, so a bad output path fails in seconds instead of "
            "after a full run."
        ) from exc

    probe_path = os.path.join(path, f".geper_write_test_{uuid.uuid4().hex}")
    try:
        with open(probe_path, "w", encoding="utf-8") as fh:
            fh.write("GEPER startup write-test probe -- safe to delete.\n")
    except OSError as exc:
        raise PipelineError(
            f"--output-dir '{path}' exists but is not writable by this process ({exc}). Fix the "
            "path or its permissions before rerunning -- this is checked once at startup, before "
            "any model load or network call, so a bad output path fails in seconds instead of "
            "after a full run."
        ) from exc
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            # Best-effort cleanup only -- the writability check itself
            # already succeeded or raised above; a leftover probe file
            # (e.g. a concurrent-deletion race) is not itself a reason
            # to fail this check.
            pass
