# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Performance benchmark for the SpliceFormer plugin's own code path
#
# NOTHING RE-RUNS THIS FILE. Measured 2026-09-11 across the 17 files matching
# geper/verify_*.py, geper/benchmark_*.py and dry_run_harness.py: ZERO are
# referenced in .github/workflows, and pytest does not collect any of them,
# because they are not named test_*. So whatever this script printed, it
# printed on the day somebody ran it by hand -- AND WHICH DAY THAT WAS IS
# RECORDED NOWHERE. `git log` on this file gives the date it was EDITED,
# which is a different fact and must not be quoted as if it were this one.
#
# WHY THE NOTE RATHER THAN A FIX: the file is not broken. Its stubs are
# honest -- it fakes everything EXCEPT the thing it verifies, and it
# propagates its exit code -- and all 59 stub targets across these harnesses
# were still defined when this was written, so they can all still be applied.
# The risk is CITATION: 'verify' is in the filename, which invites someone to
# quote this file's green as evidence. That is the `docker history` shape --
# something that reads as a record and is not one.
#
# IF YOU ARE ABOUT TO CITE THIS FILE: run it, and say when you ran it.
#
"""
Performance benchmark for the SpliceFormer plugin's own code path
(sequence preparation, one-hot encoding, and the post-inference
delta/classification summary) -- mirrors benchmark_clingen.py's /
benchmark_gnomad.py's structure and disclaimers.

WHAT'S MEASURED
-----------------
  A: `SpliceFormerPlugin._prepare_sequence` -- center/pad/truncate a
     variant's ref/alt window to the model's fixed 45,000 nt input
     length, for N synthetic variants.
  B: `SpliceFormerPlugin._one_hot_encode` -- one-hot encode a prepared
     45,000 nt window to the model's `(4, 45000)` input tensor.
  C: End-to-end `predict()` through `ModelManager`, with the actual
     `SpliceFormer.forward()` call replaced by a fast synthetic stand-
     in (see WHAT'S NOT MEASURED below) -- this isolates GEPER's own
     plugin-glue overhead (encoding + delta/classification summary +
     ModelManager's lazy-load/caching machinery) from the real
     model's own forward-pass cost, which depends entirely on the
     host's hardware (see PERFORMANCE_REPORT.md's own GPU-vs-CPU
     framing for Enformer/Borzoi -- SpliceFormer is no different).

WHAT'S NOT MEASURED
-----------------
This benchmark does NOT measure the real SpliceFormer forward pass
(the 8-layer transformer + SpliceAI-style CNN encoder over a 45,000 nt
window) or the real checkpoint download from
raw.githubusercontent.com -- this sandbox has no route to either a
GPU or a guaranteed-reachable GitHub raw-content host at benchmark
time, so any number this script could produce for either would be a
fabricated guess, not a measurement. See this project's
README.md "Whether GPU is supported" / "Memory requirements" answers
(sourced from the official paper's own reported training hardware and
the model's parameter count) for that information instead.
"""

import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import _fake_heavy_deps  # noqa: E402

_fake_heavy_deps.install()

from unittest import mock  # noqa: E402

from pipeline.models.manager import ModelManager  # noqa: E402
from pipeline.models.registry import ModelRegistry  # noqa: E402
from pipeline.models.spliceformer_plugin import SpliceFormerPlugin  # noqa: E402
from utils.profiling import StageProfiler  # noqa: E402

N_VARIANTS = 200
_WINDOW_SEQ = "ACGT" * 6000  # 24,000 nt synthetic window, deliberately
# shorter than the model's 45,000 nt input so _prepare_sequence's own
# pad/center logic (not just a pass-through) is actually exercised.


class _FastFakeSpliceFormerModel:
    """Synthetic stand-in for the real forward pass -- see this
    script's module docstring for why the real model isn't run here.
    Returns well-formed output of the correct shape so every
    downstream computation in `_infer_impl` (delta, classification,
    confidence) runs for real."""

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, features):
        import torch

        n = features.shape[0]
        out = torch.stack([torch.full((3, 8), 0.1 * (i % 3)) for i in range(n)], dim=0)
        return out, None, None, None, None


def main() -> None:
    profiler = StageProfiler()
    profiler.mark_run_start()

    with profiler.timer("prepare_sequence_x%d" % N_VARIANTS):
        prepared = [SpliceFormerPlugin._prepare_sequence(_WINDOW_SEQ) for _ in range(N_VARIANTS)]

    with profiler.timer("one_hot_encode_x%d" % N_VARIANTS):
        for seq in prepared:
            SpliceFormerPlugin._one_hot_encode(seq, strand="+")

    registry = ModelRegistry()
    registry.register("spliceformer", SpliceFormerPlugin)
    manager = ModelManager(registry=registry)

    with (
        mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config,
        mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=True),
        mock.patch(
            "pipeline.models.spliceformer_plugin.spliceformer_loader.build_model",
            return_value=_FastFakeSpliceFormerModel(),
        ),
        mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint"),
        mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.load_checkpoint_into"),
    ):
        mock_config.splicing.ENABLE_SPLICEFORMER = True
        mock_config.splicing.SPLICEFORMER_SOURCE_REF = "v1.0.0"
        mock_config.splicing.SPLICEFORMER_CHECKPOINT = "transformer_encoder_45k_171022_0"

        with profiler.timer("predict_first_call_including_load"):
            manager.predict("spliceformer", _WINDOW_SEQ, _WINDOW_SEQ)

        with profiler.timer("predict_x%d_warm" % N_VARIANTS):
            for _ in range(N_VARIANTS):
                manager.predict("spliceformer", _WINDOW_SEQ, _WINDOW_SEQ)

    profiler.mark_run_end()
    print(profiler.to_markdown(title="SpliceFormer Plugin-Glue Performance Profile"))


if __name__ == "__main__":
    start = time.time()
    main()
    print(f"\nTotal script wall-clock: {time.time() - start:.2f}s")
