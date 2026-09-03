"""
tests/test_process_control.py (kim_pipeline integration tests)
─────────────────────────────────────────────────────────────

Integration tests for process_control module with PipelineRunner.

(Shared unit tests for process_control.py live in shared/tests/test_process_control.py)
"""

from __future__ import annotations

import pytest

from shared.process_control import _CURRENT_KILL_CALLBACK


class TestPipelineRunnerSetsTheContextvar:
    def test_run_sets_current_kill_callback_from_registered_callback(self, tmp_path):
        """The other end of Bug A's fix: PipelineRunner.run() must actually
        set _CURRENT_KILL_CALLBACK to whatever register_kill_callback()
        was given, before any stage runs -- this is what makes it visible
        to _run()/spawn_tracked() no matter how deep the call stack that
        eventually spawns a subprocess (alignment, variant calling,
        reference-index building, ...).

        Short-circuits real pipeline execution by making config validation
        itself the probe: it's the first thing run() calls after setting
        the contextvar, well before any real FASTQ/reference/subprocess
        work would be needed.
        """
        import pipeline.orchestration.runner as runner_mod

        observed = {}

        def _probe_validate_config(cfg):
            observed["callback"] = _CURRENT_KILL_CALLBACK.get()
            raise RuntimeError("stop here -- probe only, not a real run")

        registered_callback = lambda proc: None  # noqa: E731

        runner = runner_mod.PipelineRunner(cfg={})
        runner.register_kill_callback(registered_callback)

        original = runner_mod.validate_config
        runner_mod.validate_config = _probe_validate_config
        try:
            with pytest.raises(RuntimeError, match="probe only"):
                runner.run(
                    fastq_r1=str(tmp_path / "r1.fastq"),
                    reference_fasta=str(tmp_path / "ref.fasta"),
                    output_dir=str(tmp_path / "out"),
                    sample_id="CTXVARTEST",
                )
        finally:
            runner_mod.validate_config = original

        assert observed.get("callback") is registered_callback

    def test_run_refuses_to_proceed_without_a_registered_callback(self, tmp_path):
        """ "Silence must fail, not pass silently" (ruled 2026-09-02): the
        original defect was a callback that was never invoked, with
        nothing to say so. run() must not repeat that shape by silently
        proceeding when nobody registered a callback at all -- that would
        just move Bug A's silence from "callback never fires" to "callback
        never got registered," which is the same failure from the
        caller's point of view: an unkillable run nobody was told about.
        """
        import pipeline.orchestration.runner as runner_mod

        runner = runner_mod.PipelineRunner(cfg={})
        # Deliberately NOT calling register_kill_callback().

        with pytest.raises(RuntimeError, match="register_kill_callback"):
            runner.run(
                fastq_r1=str(tmp_path / "r1.fastq"),
                reference_fasta=str(tmp_path / "ref.fasta"),
                output_dir=str(tmp_path / "out"),
                sample_id="UNREGISTEREDTEST",
            )

    def test_no_kill_tracking_is_an_explicit_opt_out_not_a_default(self, tmp_path):
        """A caller that genuinely doesn't need cancellation (CLI/script
        use, no DELETE endpoint) must say so by name -- NO_KILL_TRACKING
        -- rather than run() silently tolerating an unregistered callback
        for everyone. Confirms passing it clears the refusal path (proven
        via the same validate_config probe as the registered-callback
        test above, so this doesn't require a real pipeline run either)."""
        import pipeline.orchestration.runner as runner_mod

        observed = {}

        def _probe_validate_config(cfg):
            observed["callback"] = _CURRENT_KILL_CALLBACK.get()
            raise RuntimeError("stop here -- probe only, not a real run")

        runner = runner_mod.PipelineRunner(cfg={})
        runner.register_kill_callback(runner_mod.NO_KILL_TRACKING)

        original = runner_mod.validate_config
        runner_mod.validate_config = _probe_validate_config
        try:
            with pytest.raises(RuntimeError, match="probe only"):
                runner.run(
                    fastq_r1=str(tmp_path / "r1.fastq"),
                    reference_fasta=str(tmp_path / "ref.fasta"),
                    output_dir=str(tmp_path / "out"),
                    sample_id="NOKILLTRACKINGTEST",
                )
        finally:
            runner_mod.validate_config = original

        assert observed.get("callback") is runner_mod.NO_KILL_TRACKING
        # And it's genuinely inert when actually invoked.
        assert runner_mod.NO_KILL_TRACKING(object()) is None
