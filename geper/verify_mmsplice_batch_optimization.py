"""
Before/after benchmark for the MMSplice execution-speed optimization
(orchestrator now calls the existing `MMSpliceService.predict_batch()`
up front instead of `MMSpliceService.predict()` once per variant --
see `pipeline/orchestrator.py::_prefetch_mmsplice_results` and the
`config.py::MMSpliceConfig.ENABLE_PREFETCH` flag).

WHAT'S COMPARED
-----------------
  BEFORE : `MMSplicePredictor.predict()` called once per variant, in a
           loop -- the orchestrator's behavior before this change.
           Each call scores ref+alt through all 5 MMSplice submodels
           (`MMSpliceModel.score_modular`), i.e. 10 Keras `.predict()`
           calls per variant.
  AFTER  : `MMSplicePredictor.predict_batch()` called once for the
           whole variant list -- the orchestrator's new prefetch
           behavior. Ref and alt windows for every variant are scored
           with exactly 2 Keras `.predict()` calls per module (one for
           all ref windows, one for all alt windows) = 10 calls total,
           not 10 x N.

Neither `predict()` nor `predict_batch()` is touched by this change --
both already existed, are already unit-tested for equivalence
(`tests/test_mmsplice_predictor.py::test_predict_batch_matches_predict_per_item`),
and are exercised unmodified here. Only the *call pattern* changed.

WHY A MOCKED KERAS LAYER
--------------------------
This sandbox has no real TensorFlow/Keras runtime (no GPU, and the
five official `.h5` MMSplice weight files are a real-network download
this environment's egress allowlist doesn't reach). `MMSpliceModel` is
therefore replaced with a `unittest.mock.Mock` whose `score_modular` /
`score_modular_batch` methods sleep for `PER_CALL_OVERHEAD_SECONDS` per
underlying Keras `.predict()` call before returning deterministic
scores -- i.e. call-count is real and drives the measured wall time,
while the per-call cost is a documented, literature-typical stand-in
for real TF/Keras dispatch overhead (small-input Keras `.predict()`
calls are dominated by fixed per-call graph-dispatch overhead, not by
FLOPs, for MMSplice's small (<100-parameter-input) submodels -- this is
exactly the cost `score_modular_batch`'s own docstring identifies as
"where MMSplice's real per-call overhead lives"). Run this file's
logic against the real weights on real hardware for a final production
number; this produces an apples-to-apples call-count comparison in the
interim, using the exact same production code path in both arms.
"""

import time
from typing import List
from unittest import mock

from pipeline.models.mmsplice.models import ModularScores
from pipeline.models.mmsplice.predictor import MMSplicePredictor

PER_CALL_OVERHEAD_SECONDS = 0.03  # documented assumption -- see module docstring
N_VARIANTS = 200


def _make_mock_model_unbatched() -> mock.Mock:
    model = mock.Mock()
    model.model_version = "mmsplice-2.4.0-test"

    def _score_modular(window, overhang):
        time.sleep(PER_CALL_OVERHEAD_SECONDS * 5)  # 5 submodels, one Keras call each
        return ModularScores(0.1, 0.1, 0.1, 0.1, 0.1)

    model.score_modular.side_effect = _score_modular
    model.score_single_module_batch.return_value = [0.0] * 41
    return model


def _make_mock_model_batched() -> mock.Mock:
    model = mock.Mock()
    model.model_version = "mmsplice-2.4.0-test"

    def _score_modular_batch(windows: List[str], overhang):
        time.sleep(PER_CALL_OVERHEAD_SECONDS * 5)  # 5 submodels, ONE Keras call each, for the whole list
        return [ModularScores(0.1, 0.1, 0.1, 0.1, 0.1) for _ in windows]

    model.score_modular_batch.side_effect = _score_modular_batch
    model.score_single_module_batch.return_value = [0.0] * 41
    return model


def run_before(n: int) -> float:
    model = _make_mock_model_unbatched()
    predictor = MMSplicePredictor(model)
    ref_windows = ["N" * 300] * n
    alt_windows = ["N" * 300] * n

    start = time.time()
    for ref_window, alt_window in zip(ref_windows, alt_windows):
        predictor.predict(ref_window, alt_window, overhang=(100, 100))
    return time.time() - start


def run_after(n: int) -> float:
    model = _make_mock_model_batched()
    predictor = MMSplicePredictor(model)
    ref_windows = ["N" * 300] * n
    alt_windows = ["N" * 300] * n

    start = time.time()
    predictor.predict_batch(ref_windows, alt_windows, overhang=(100, 100))
    return time.time() - start


if __name__ == "__main__":
    before_s = run_before(N_VARIANTS)
    after_s = run_after(N_VARIANTS)
    print(f"MMSplice scoring stage, {N_VARIANTS} variants (simulated {PER_CALL_OVERHEAD_SECONDS * 1000:.0f}ms/Keras-call dispatch overhead):")
    print(f"  BEFORE (predict() x {N_VARIANTS}, unbatched):      {before_s:.2f}s")
    print(f"  AFTER  (predict_batch() x 1):               {after_s:.2f}s")
    print(f"  Speedup on MMSplice's Keras-call dispatch cost: {before_s / after_s:.1f}x")
