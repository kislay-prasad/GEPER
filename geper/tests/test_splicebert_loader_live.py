"""
Real-checkpoint regression test for a genuine bug found via a live
Colab run of this project: `pipeline/models/splicebert/loader.py
::build_model_and_tokenizer` never returned (observed as an
indefinite hang, not a crash) when loading the real SpliceBERT.1024nt
checkpoint under `transformers` 4.x. Originally attributed to
`transformers`' TensorFlow-backend auto-detection, and "fixed" first
with `USE_TF=0`, then with a direct override of an internal
`transformers` flag -- ROUND 18 CORRECTION: both attributions are now
known false. Confirmed live under this repo's current pin
(`transformers>=5.12.1,<6.0.0`, installed `5.13.1`): the internal flag
those fixes touched (`transformers.utils.import_utils._tf_available`)
and TensorFlow's own Auto* entry point (`transformers.TFAutoModel`)
both no longer exist under `transformers` v5 at all -- v5 dropped
TensorFlow support entirely, so neither fix could have been doing
anything under the environment this project actually runs. Both were
removed from `loader.py` this round. See
`pipeline/models/splicebert/loader.py::build_model_and_tokenizer`'s
own docstring for what is actually known about the still-real timeout
this test guards against: the load succeeds quickly in isolation, but
has repeatedly stalled inside the full orchestrator process
specifically; the triggering import/call has not been isolated.

This test is intentionally NOT mocked (unlike test_splicebert_plugin.py's
existing suite) -- it exercises the real checkpoint (already downloaded
to plugin_model_cache/splicebert/ elsewhere in this project's own
testing) through a hard wall-clock timeout, so a regression of this
exact bug fails fast and loud instead of hanging the test run silently.
Skips (rather than fails) if the checkpoint isn't present on disk in
this environment -- unlike the "_live" network tests elsewhere in this
project, this doesn't re-download it (a ~208MB Zenodo archive), it only
exercises the loading step against whatever is already cached. NOTE:
when the checkpoint IS present (as it was found to be in this round's
own dev sandbox), running this test loads real model weights -- do not
run it as part of a routine/scoped offline pass on a memory-constrained
machine; it was run exactly once this round, confirmed the load still
completes correctly with both stale fixes removed, and was not re-run.
"""

import multiprocessing
import unittest
from pathlib import Path

from config import CONFIG

_CHECKPOINT_DIR = Path(CONFIG.splicing.PLUGIN_CACHE_DIR) / "splicebert" / CONFIG.splicing.SPLICEBERT_CHECKPOINT
_TIMEOUT_SECS = 60


def _load_in_subprocess(checkpoint_dir: str, result_queue) -> None:
    """Runs in a fresh child process so a real hang can be killed from outside rather than wedging the test runner itself."""
    try:
        from pipeline.models.splicebert.loader import build_model_and_tokenizer

        model, tokenizer = build_model_and_tokenizer(Path(checkpoint_dir))
        result_queue.put(("ok", type(model).__name__, type(tokenizer).__name__))
    except Exception as exc:  # noqa: BLE001 - report any failure back to the parent, not just success
        result_queue.put(("error", str(exc), None))


@unittest.skipUnless(
    (_CHECKPOINT_DIR / "config.json").is_file() and (_CHECKPOINT_DIR / "pytorch_model.bin").is_file(),
    f"SpliceBERT checkpoint not present at {_CHECKPOINT_DIR} in this environment (not re-downloaded by this test -- see module docstring)",
)
class TestSpliceBertLoaderDoesNotHang(unittest.TestCase):
    def test_build_model_and_tokenizer_completes_within_timeout(self):
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        proc = ctx.Process(target=_load_in_subprocess, args=(str(_CHECKPOINT_DIR), result_queue))
        proc.start()
        proc.join(timeout=_TIMEOUT_SECS)

        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            self.fail(
                f"build_model_and_tokenizer() did not return within {_TIMEOUT_SECS}s -- this is exactly "
                f"the real load stall this test guards against. As of round 18, the cause is NOT "
                f"TensorFlow-backend auto-detection (that attribution was confirmed false under this "
                f"repo's transformers v5 pin -- see this module's own docstring and "
                f"pipeline/models/splicebert/loader.py::build_model_and_tokenizer's docstring for what "
                f"is actually known). Do not reintroduce USE_TF=0 as a fix on the strength of this "
                f"failure alone."
            )

        self.assertFalse(result_queue.empty(), "subprocess exited without reporting a result")
        status, a, b = result_queue.get()
        self.assertEqual(status, "ok", msg=f"loading failed with: {a}")
        self.assertEqual(a, "BertForMaskedLM")
        self.assertEqual(b, "BertTokenizerFast")


if __name__ == "__main__":
    unittest.main()
