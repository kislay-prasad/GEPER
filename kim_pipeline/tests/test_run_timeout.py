"""
tests/test_run_timeout.py
───────────────────────────
Covers the configurable subprocess timeout added to the shared
`_run()` helper in `pipeline/fastq/errors.py`.

Previously `_run()` called `subprocess.run()` with no `timeout=` at
all, so a stalled or pathologically slow external tool (bwa, samtools,
bcftools, freebayes, ...) could hang a pipeline run indefinitely with
nothing to recover it. These tests confirm:

  * The new default (60 minutes) is wired into `subprocess.run()`
    without any caller needing to change.
  * A command that exceeds its timeout is killed and raises a
    `FastqPipelineError` that clearly identifies the stage, the
    command, and the timeout duration -- never a raw
    `subprocess.TimeoutExpired` escaping to the caller.
  * `timeout_seconds=None` still allows indefinite waiting (the
    previous behavior), for a caller that legitimately needs it.
  * Every existing behavior for commands that complete successfully,
    or that fail with a non-zero exit code, is unchanged.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.fastq.errors import (
    FastqPipelineError,
    _DEFAULT_TIMEOUT_SECONDS,
    _run,
)


def test_default_timeout_is_sixty_minutes():
    assert _DEFAULT_TIMEOUT_SECONDS == 60 * 60


def test_default_timeout_is_forwarded_to_subprocess_run():
    """Every existing call site (none of which pass timeout_seconds)
    must now get the 60-minute default automatically, with no changes
    required at the call sites themselves."""
    fake_proc = mock.Mock(returncode=0, stdout="ok", stderr="")
    with mock.patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = _run(["echo", "hi"], stage="test.stage")

    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["timeout"] == _DEFAULT_TIMEOUT_SECONDS
    assert result.returncode == 0
    assert result.stdout == "ok"


def test_custom_timeout_is_forwarded_to_subprocess_run():
    fake_proc = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake_proc) as mock_run:
        _run(["echo", "hi"], stage="test.stage", timeout_seconds=120.0)

    _, kwargs = mock_run.call_args
    assert kwargs["timeout"] == 120.0


def test_timeout_none_disables_the_timeout():
    """An explicit opt-out for a caller that legitimately needs to
    wait indefinitely -- matches the previous, unbounded behavior."""
    fake_proc = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake_proc) as mock_run:
        _run(["echo", "hi"], stage="test.stage", timeout_seconds=None)

    _, kwargs = mock_run.call_args
    assert kwargs["timeout"] is None


def test_timeout_expired_raises_fastq_pipeline_error_with_full_context():
    cmd = ["freebayes", "-f", "ref.fasta", "aligned.bam"]
    timeout_exc = subprocess.TimeoutExpired(cmd=cmd, timeout=1800.0)

    with mock.patch("subprocess.run", side_effect=timeout_exc):
        with pytest.raises(FastqPipelineError) as exc_info:
            _run(cmd, stage="variant_calling.freebayes", timeout_seconds=1800.0)

    exc = exc_info.value
    # Stage identification.
    assert exc.stage == "variant_calling.freebayes"
    # Tool identification (first element of the command).
    assert exc.tool == "freebayes"
    # The full command that timed out must be identifiable in the message.
    message = str(exc)
    assert "freebayes" in message
    assert "ref.fasta" in message
    assert "aligned.bam" in message
    # The timeout duration must be identifiable in the message.
    assert "1800" in message
    # And the stage must also appear in the rendered __str__ (via the
    # existing FastqPipelineError.__str__ formatting).
    assert "variant_calling.freebayes" in message


def test_timeout_expired_is_a_real_kill_not_just_an_error_message():
    """End-to-end (no mocking of subprocess.run itself): a real
    process that outlives its timeout is actually killed and raises
    FastqPipelineError, exercising the genuine
    subprocess.TimeoutExpired path rather than a simulated one."""
    with pytest.raises(FastqPipelineError) as exc_info:
        _run(
            ["sleep", "5"],
            stage="test.real_timeout",
            timeout_seconds=0.2,
        )

    exc = exc_info.value
    assert exc.stage == "test.real_timeout"
    assert exc.tool == "sleep"
    assert "0" in str(exc)  # rendered timeout duration


def test_successful_command_behavior_is_unchanged():
    """Regression guard: adding the timeout parameter must not affect
    the success path at all."""
    result = _run(["echo", "-n", "hello"], stage="test.stage")
    assert result.returncode == 0
    assert result.stdout == "hello"


def test_nonzero_exit_still_raises_fastq_pipeline_error_unchanged():
    """Regression guard: the pre-existing non-zero-exit-code error
    path (unrelated to timeouts) must be completely unaffected."""
    with pytest.raises(FastqPipelineError) as exc_info:
        _run(["python3", "-c", "import sys; sys.exit(3)"], stage="test.stage")

    exc = exc_info.value
    assert exc.stage == "test.stage"
    assert "failed (exit 3)" in str(exc)


def test_missing_executable_still_raises_fastq_pipeline_error_unchanged():
    """Regression guard: the pre-existing FileNotFoundError path is
    completely unaffected by the timeout addition."""
    with pytest.raises(FastqPipelineError) as exc_info:
        _run(["this-tool-does-not-exist-anywhere"], stage="test.stage")

    assert "not found" in str(exc_info.value).lower()
